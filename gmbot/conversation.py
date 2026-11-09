"""Per-target idle/active state and parsing of the LLM's reply/skip/end output."""

from __future__ import annotations

import re
import time
from difflib import SequenceMatcher
from dataclasses import dataclass, field

from .history import END, SKIP

REMEMBER_RE = re.compile(r"\[REMEMBER:\s*([^\]]*)\]", re.IGNORECASE)


def mentions(text: str, names: list[str]) -> bool:
    """True if any of the names appears as a whole word in text (case-insensitive)."""
    for name in names:
        if name and re.search(rf"(?<![\w\[\]\\`^{{}}|-]){re.escape(name)}(?![\w\[\]\\`^{{}}|-])", text, re.IGNORECASE):
            return True
    return False


@dataclass
class Decision:
    text: str | None  # line to send, if any
    end: bool  # conversation over, go idle
    memories: list[str] = field(default_factory=list)  # facts to store in long-term memory


def parse_reply(raw: str, nick: str, max_chars: int) -> Decision:
    memories = [m.strip() for m in REMEMBER_RE.findall(raw) if m.strip()]
    raw = REMEMBER_RE.sub(" ", raw)
    end = END in raw.upper()
    text = re.sub(r"\[(SKIP|END)\]", " ", raw, flags=re.IGNORECASE)
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = re.sub(rf"^(<{re.escape(nick)}>|{re.escape(nick)}\s*:)\s*", "", line, flags=re.IGNORECASE)
    line = " ".join(line.split())
    if len(line) > max_chars:
        cut = line[:max_chars]
        line = (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(",;:")
    return Decision(text=line or None, end=end, memories=memories)


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def is_repeat(text: str, previous: list[str], threshold: float) -> bool:
    """True if text is (nearly) the same as one of the previous lines, ignoring case and punctuation."""
    norm = _normalize(text)
    for prev in previous:
        other = _normalize(prev)
        if norm == other or (threshold < 1 and SequenceMatcher(None, norm, other).ratio() >= threshold):
            return True
    return False


class Conversation:
    """Tracks whether the bot is currently taking part in a target's conversation."""

    def __init__(self, timeout: float):
        self.timeout = timeout
        self.active = False
        self.last_involved = 0.0

    def activate(self) -> None:
        self.active = True
        self.last_involved = time.monotonic()

    def touch(self) -> None:
        self.last_involved = time.monotonic()

    def end(self) -> None:
        self.active = False

    def expired(self) -> bool:
        return self.active and time.monotonic() - self.last_involved > self.timeout
