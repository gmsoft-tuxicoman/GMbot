"""Per-target message history and LLM prompt construction."""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

SKIP = "[SKIP]"
END = "[END]"

PROTOCOL_RULES = f"""\
## how this chat works
- you are {{nick}}, chatting on irc ({{network}}) in {{where}}
- each user message is formatted as "<nick> text"; lines starting with "* nick" are /me actions
- you got pulled into this conversation because someone mentioned you. keep taking part while it involves you, without needing to be pinged again
- reply with exactly ONE short line, usually well under 150 characters. never multiple lines
- write like a regular person on irc: mostly lowercase, casual, light or no punctuation, abbreviations are fine. no markdown, no lists, no headings, no code blocks, don't sign your messages, don't start with your own nick or "<{{nick}}>"
- to address someone specific you can start with "theirnick: "
- if the latest messages aren't meant for you or you have nothing worth adding, output only {SKIP}
- if the conversation with you is over (they said bye/thanks and moved on, they're talking to someone else now, the topic no longer involves you), output {END}. you can put a short last line before it, like "np, cya {END}"
- {SKIP} and {END} are control words, use them only as described
{{memory_rules}}
current time: {{now}} UTC
people in {{where}}: {{members}}
"""


@dataclass
class Entry:
    nick: str
    text: str
    is_bot: bool = False
    is_action: bool = False
    ts: float = field(default_factory=time.time)

    def render(self) -> str:
        if self.is_bot:
            return f"* {self.nick} {self.text}" if self.is_action else self.text
        return f"* {self.nick} {self.text}" if self.is_action else f"<{self.nick}> {self.text}"


class History:
    def __init__(self, max_entries: int):
        self.max_entries = max_entries
        self._logs: dict[str, deque[Entry]] = {}

    def add(self, target: str, entry: Entry) -> None:
        self._logs.setdefault(target, deque(maxlen=self.max_entries)).append(entry)

    def entries(self, target: str) -> list[Entry]:
        return list(self._logs.get(target, ()))


class Instructions:
    """INSTRUCTIONS.md contents, re-read whenever the file changes."""

    def __init__(self, path: Path):
        self.path = path
        self._mtime: float | None = None
        self._text = ""

    @property
    def text(self) -> str:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._mtime is not None:
                log.warning("instructions file %s disappeared, keeping last version", self.path)
            return self._text
        if mtime != self._mtime:
            self._text = self.path.read_text(encoding="utf-8").strip()
            self._mtime = mtime
            log.info("loaded instructions from %s", self.path)
        return self._text


def estimate_tokens(text: str) -> int:
    return len(text) // 4 + 4


MEMORY_RULES = """\
- you have a long-term memory (below). when someone tells you a fact worth knowing later (about \
themselves, other people, the channel, projects, preferences), append [REMEMBER: the fact] to your \
reply, written as a short standalone sentence with names, e.g. "[REMEMBER: alice runs gentoo on her laptop]". \
don't store trivia, jokes, secrets/passwords or things already in memory. it's invisible to others

## your memory
{memory}
"""


def system_prompt(
    instructions: str, nick: str, network: str, where: str, members: list[str], memory: str | None = None
) -> str:
    """memory=None disables the memory feature; "" means enabled but nothing remembered yet."""
    memory_rules = "" if memory is None else MEMORY_RULES.format(memory=memory or "(nothing yet)")
    rules = PROTOCOL_RULES.format(
        memory_rules=memory_rules,
        nick=nick,
        network=network,
        where=where,
        now=time.strftime("%Y-%m-%d %H:%M", time.gmtime()),
        members=", ".join(members) if members else "(unknown)",
    )
    return f"{instructions}\n\n{rules}" if instructions else rules


def build_messages(system: str, entries: list[Entry], budget_tokens: int, reply_tokens: int) -> list[dict]:
    """Map history to chat messages, dropping the oldest entries to fit the token budget."""
    remaining = budget_tokens - reply_tokens - estimate_tokens(system)
    kept: list[Entry] = []
    for e in reversed(entries):
        cost = estimate_tokens(e.render())
        if cost > remaining:
            break
        remaining -= cost
        kept.append(e)
    kept.reverse()
    # Chat templates generally want the conversation to start with a user turn.
    while kept and kept[0].is_bot:
        kept.pop(0)

    messages: list[dict] = [{"role": "system", "content": system}]
    for e in kept:
        role = "assistant" if e.is_bot else "user"
        if messages[-1]["role"] == role:
            messages[-1]["content"] += "\n" + e.render()
        else:
            messages.append({"role": role, "content": e.render()})
    return messages
