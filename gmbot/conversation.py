"""Per-target idle/active state and parsing of the LLM's reply/skip/end output."""

from __future__ import annotations

import re
import time
from difflib import SequenceMatcher
from dataclasses import dataclass

from .history import END, SKIP

ACTION_PREFIX = "/me "  # marks a reply line to be sent as a CTCP ACTION


def mentions(text: str, names: list[str]) -> bool:
    """True if any of the names appears as a whole word in text (case-insensitive)."""
    for name in names:
        if name and re.search(rf"(?<![\w\[\]\\`^{{}}|-]){re.escape(name)}(?![\w\[\]\\`^{{}}|-])", text, re.IGNORECASE):
            return True
    return False


@dataclass
class Decision:
    lines: list[str]  # messages to send, one per line of the reply (empty = say nothing)
    end: bool  # conversation over, go idle

    @property
    def text(self) -> str | None:
        return "\n".join(self.lines) or None


def _fits(line: str, max_chars: int, max_bytes: int) -> int:
    """How many characters of line fit in max_chars and (if set) max_bytes of UTF-8."""
    n = min(len(line), max_chars)
    if max_bytes:
        n = min(n, len(line.encode()[:max_bytes].decode(errors="ignore")))
    return n


def wrap(line: str, max_chars: int, max_bytes: int = 0) -> list[str]:
    """Split a line into chunks of at most max_chars (and max_bytes, if set), at word boundaries
    when possible."""
    chunks: list[str] = []
    while (limit := _fits(line, max_chars, max_bytes)) < len(line):
        cut = line.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(line[:cut].rstrip())
        line = line[cut:].lstrip()
    if line:
        chunks.append(line)
    return chunks


# A "[14:32] " the model copied from the history format.
TIMESTAMP_RE = re.compile(r"^\[\d{1,2}:\d{2}(:\d{2})?\]\s*")


def parse_reply(raw: str, nick: str, max_chars: int, max_bytes: int = 0) -> Decision:
    """max_bytes: what fits in one IRC message (0 = no limit); /me lines get 9 bytes less for
    the CTCP framing."""
    end = END in raw.upper()
    text = re.sub(r"\[(SKIP|END)\]", " ", raw, flags=re.IGNORECASE)
    echo = re.compile(rf"^(<{re.escape(nick)}>|{re.escape(nick)}\s*:)\s*", re.IGNORECASE)
    lines: list[str] = []
    action = re.compile(rf"^(/me\s+|\*\s*{re.escape(nick)}\s+)", re.IGNORECASE)
    for line in text.splitlines():
        line = " ".join(echo.sub("", TIMESTAMP_RE.sub("", line.strip())).split())
        if not line or re.fullmatch(r"`{3,}\w*", line):  # skip empty lines and code fences
            continue
        # "/me does x" (or "* nick does x", copied from how others' actions look) is an action.
        if m := action.match(line):
            action_bytes = max(max_bytes - len("\x01ACTION \x01"), 1) if max_bytes else 0
            lines.extend(ACTION_PREFIX + chunk for chunk in wrap(line[m.end() :], max_chars, action_bytes))
        else:
            lines.extend(wrap(line, max_chars, max_bytes))
    return Decision(lines=lines, end=end)


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
        self.answered_seq = 0  # history seq of the last message the model has already responded to
        self.crossed = 0  # messages sent while the bot's last reply was being written

    def activate(self) -> None:
        self.active = True
        self.last_involved = time.monotonic()

    def touch(self) -> None:
        self.last_involved = time.monotonic()

    def end(self) -> None:
        self.active = False

    def expired(self) -> bool:
        return self.active and time.monotonic() - self.last_involved > self.timeout
