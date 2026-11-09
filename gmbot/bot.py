"""Glue between the IRC client, the history and the LLM."""

from __future__ import annotations

import asyncio
import logging
import signal

from .config import Config
from .conversation import Conversation, mentions, parse_reply
from .history import Entry, History, Instructions, build_messages, system_prompt
from .irc import IRCClient, irc_lower, is_channel, strip_formatting
from .llm import LLMClient
from .memory import Memory

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.irc = IRCClient(cfg.irc, cfg.bot.ctcp_version)
        self.irc.on_message = self.on_message
        self.llm = LLMClient(cfg.llm)
        self.history = History(cfg.bot.context_messages)
        self.instructions = Instructions(cfg.instructions_path)
        self.memory = Memory(cfg.memory_path, cfg.bot.memory_max_facts) if cfg.bot.memory_enabled else None
        self.ignore = {irc_lower(n) for n in cfg.bot.ignore}
        self._convs: dict[str, Conversation] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, asyncio.Task] = {}

    async def run(self) -> None:
        self.instructions.text  # load early so a bad path shows up at startup
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        irc_task = asyncio.create_task(self.irc.run())
        stop_task = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({irc_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                await self.irc.quit(self.cfg.irc.quit_message)
                irc_task.cancel()
            await asyncio.gather(irc_task, return_exceptions=stop.is_set())
        finally:
            stop_task.cancel()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            await self.llm.aclose()

    def _conv(self, key: str) -> Conversation:
        if key not in self._convs:
            self._convs[key] = Conversation(self.cfg.bot.conversation_timeout)
        return self._convs[key]

    async def on_message(self, nick: str, target: str, text: str, is_action: bool) -> None:
        key = irc_lower(target)
        text = strip_formatting(text)
        self.history.add(key, Entry(nick=nick, text=text, is_action=is_action))
        if irc_lower(nick) in self.ignore or irc_lower(nick) == irc_lower(self.irc.nick):
            return

        conv = self._conv(key)
        if conv.expired():
            log.info("[%s] conversation timed out, going idle", target)
            conv.end()

        if not is_channel(target) or mentions(text, [self.irc.nick, *self.cfg.bot.aliases]):
            if not conv.active:
                log.info("[%s] mentioned by %s, joining conversation", target, nick)
            conv.activate()
        elif not conv.active:
            return

        # Debounce: restart the timer while people are still typing.
        pending = self._pending.get(key)
        if pending and not pending.done():
            pending.cancel()
        self._pending[key] = asyncio.create_task(self._respond_later(key, target))

    async def _respond_later(self, key: str, target: str) -> None:
        await asyncio.sleep(self.cfg.bot.debounce_seconds)
        # From here on, new messages must not cancel the generation; they schedule a new turn.
        self._pending.pop(key, None)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            conv = self._conv(key)
            if not conv.active:
                return
            try:
                await self._respond(key, target, conv)
            except Exception:
                log.exception("[%s] failed to respond", target)

    async def _respond(self, key: str, target: str, conv: Conversation) -> None:
        where = target if is_channel(target) else f"a private chat with {target}"
        members = self.irc.channel_members(target) if is_channel(target) else [target, self.irc.nick]
        memory = self.memory.text if self.memory else None
        system = system_prompt(self.instructions.text, self.irc.nick, self.cfg.irc.server, where, members, memory)
        messages = build_messages(
            system,
            self.history.entries(key),
            self.cfg.llm.context_budget_tokens,
            self.cfg.llm.max_tokens,
        )
        raw = await self.llm.chat(messages)
        if raw is None:
            return
        log.debug("[%s] llm raw: %r", target, raw)
        decision = parse_reply(raw, self.irc.nick, self.cfg.bot.max_reply_chars)
        if self.memory:
            speaker = next((e.nick for e in reversed(self.history.entries(key)) if not e.is_bot), "?")
            for fact in decision.memories:
                self.memory.add(fact, speaker, target if is_channel(target) else "pm")
        if decision.text:
            sent = self.irc.privmsg(target, decision.text)
            self.history.add(key, Entry(nick=self.irc.nick, text=sent, is_bot=True))
            conv.touch()
            log.info("[%s] <%s> %s", target, self.irc.nick, sent)
        elif not decision.end:
            log.info("[%s] skipped", target)
        if decision.end:
            log.info("[%s] conversation ended by llm, going idle", target)
            conv.end()
