"""Long-term memory: facts the LLM chose to remember, stored as a Markdown list in MEMORY.md."""

from __future__ import annotations

import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

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
