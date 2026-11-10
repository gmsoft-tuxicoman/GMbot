"""Long-term memory: facts the LLM chose to remember, stored as a Markdown list in MEMORY.md."""

from __future__ import annotations

import logging
import re
import time
from difflib import SequenceMatcher
from pathlib import Path

log = logging.getLogger(__name__)

# How close a [FORGET: ...] must be to a stored fact for that fact to be removed.
FORGET_SIMILARITY = 0.8


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _fact_text(line: str) -> str:
    """'- fact (nick, #chan, date)' -> 'fact'"""
    return re.sub(r"\s*\([^()]*\)\s*$", "", line[2:])


HEADER = "# memory\n\nfacts the bot learned in conversations. edit freely, one fact per line.\n\n"


class Memory:
    def __init__(self, path: Path, max_facts: int):
        self.path = path
        self.max_facts = max_facts
        self._mtime: float | None = None
        self._lines: list[str] = []

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime, self._lines = None, []
            return
        if mtime != self._mtime:
            text = self.path.read_text(encoding="utf-8")
            self._lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
            self._mtime = mtime

    @property
    def text(self) -> str:
        """The remembered facts, one per line, for the system prompt."""
        self._load()
        return "\n".join(self._lines)

    @property
    def facts(self) -> list[str]:
        self._load()
        return [ln[2:] for ln in self._lines]

    def forget(self, pattern: str) -> int:
        """Remove facts containing pattern (case-insensitive); returns how many were removed."""
        self._load()
        pattern = pattern.lower()
        kept = [ln for ln in self._lines if pattern not in ln.lower()]
        removed = len(self._lines) - len(kept)
        if removed:
            self._lines = kept
            self._save()
        return removed

    def forget_fact(self, text: str) -> str | None:
        """Remove the single fact best matching text, if it's close enough; returns the removed fact."""
        self._load()
        wanted = _norm(_fact_text("- " + text))
        if not wanted:
            return None
        best, best_score = None, 0.0
        for i, line in enumerate(self._lines):
            score = SequenceMatcher(None, wanted, _norm(_fact_text(line))).ratio()
            if score > best_score:
                best, best_score = i, score
        if best is None or best_score < FORGET_SIMILARITY:
            log.info("nothing close enough to forget for: %s", text)
            return None
        removed = self._lines.pop(best)[2:]
        self._save()
        log.info("forgot: %s", removed)
        return removed

    def _save(self) -> None:
        self.path.write_text(HEADER + "\n".join(self._lines) + ("\n" if self._lines else ""), encoding="utf-8")
        self._mtime = self.path.stat().st_mtime

    def add(self, fact: str, nick: str, where: str) -> bool:
        """Append a fact; returns False if it was empty or already known."""
        fact = " ".join(fact.split()).strip(" .")
        if not fact:
            return False
        self._load()
        known = {ln[2:].split(" (", 1)[0].lower() for ln in self._lines}
        if fact.lower() in known:
            return False
        self._lines.append(f"- {fact} ({nick}, {where}, {time.strftime('%Y-%m-%d', time.gmtime())})")
        if len(self._lines) > self.max_facts:
            self._lines = self._lines[-self.max_facts :]
        self._save()
        log.info("remembered: %s", fact)
        return True
