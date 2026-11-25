"""Glue between the IRC client, the history and the LLM."""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from .commands import Commands
from .config import Config
from .conversation import ACTION_PREFIX, Conversation, Decision, is_repeat, mentions, parse_reply
from .history import Entry, History, Instructions, build_messages, context_note, system_prompt
from .irc import IRCClient, irc_lower, is_channel, strip_formatting
from .llm import LLMClient
from .memory import Digest, Memory, apply_compact, compact_messages, speakers

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.irc = IRCClient(cfg.irc, cfg.bot.ctcp_version)
        self.irc.on_message = self.on_message
        self.irc.on_nick_change = self.on_nick_change
        self.llm = LLMClient(cfg.llm)
        self.history = History(cfg.bot.context_messages)
        self.instructions = Instructions(cfg.instructions_path)
        self.memory = (
            Memory(cfg.memory_path, cfg.bot.memory_max_facts, cfg.bot.memory_journal_max)
            if cfg.bot.memory_enabled
            else None
        )
        self.ignore = {irc_lower(n) for n in cfg.bot.ignore}
        self.convs: dict[str, Conversation] = {}
        self.muted: set[str] = set()
        self.commands = Commands(self)
        self._stop = asyncio.Event()
        self._quit_message = cfg.irc.quit_message
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, asyncio.Task] = {}
        self._digest_lock = asyncio.Lock()  # one summary pass at a time
        self._digest_retry: dict[str, float] = {}  # target -> earliest time to retry a failed pass

    async def run(self) -> None:
        self.instructions.text  # load early so a bad path shows up at startup
        stop = self._stop
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        irc_task = asyncio.create_task(self.irc.run())
        stop_task = asyncio.create_task(stop.wait())
        memory_task = asyncio.create_task(self._memory_loop()) if self.memory else None
        try:
            await asyncio.wait({irc_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if stop.is_set():
                await self.irc.quit(self._quit_message)
                irc_task.cancel()
                if memory_task:
                    memory_task.cancel()
                    await self._flush_memory()
            await asyncio.gather(irc_task, return_exceptions=stop.is_set())
        finally:
            stop_task.cancel()
            if memory_task:
                memory_task.cancel()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
            await self.llm.aclose()

    def request_quit(self, message: str) -> None:
        self._quit_message = message
        self._stop.set()

    def reset(self, key: str, clear_history: bool = True) -> None:
        """Go idle in a target, optionally dropping its stored conversation."""
        if pending := self._pending.pop(key, None):
            pending.cancel()
        if key in self.convs:
            self.convs[key].end()
        if clear_history:
            self.history.clear(key)

    def say(self, target: str, text: str, action: bool = False) -> None:
        """Send a line as the bot (from an owner command) and record it in the history."""
        text = self.irc.action(target, text) if action else self.irc.privmsg(target, text)
        self.history.add(irc_lower(target), Entry(nick=self.irc.nick, text=text, is_bot=True, is_action=action))

    def _conv(self, key: str) -> Conversation:
        if key not in self.convs:
            self.convs[key] = Conversation(self.cfg.bot.conversation_timeout)
        return self.convs[key]

    def on_nick_change(self, old: str, new: str, channels: list[str]) -> None:
        """Record real nick changes so the model learns them from the server, not from claims in chat."""
        for chan in channels:
            self.history.add(irc_lower(chan), Entry(nick=old, text=f"is now known as {new}", is_action=True))
        for key in (irc_lower(old), irc_lower(new)):
            if key in self.convs or self.history.entries(key):
                self.history.add(key, Entry(nick=old, text=f"is now known as {new}", is_action=True))
                break

    async def on_message(
        self, nick: str, target: str, text: str, is_action: bool, account: str | None = None
    ) -> None:
        key = irc_lower(target)
        text = strip_formatting(text)
        if not is_action and self.commands.is_command(text):
            # Commands never reach the history or the LLM: the owner's are run, anyone else's are
            # dropped so they can't steer the model (e.g. ".nick x" making it call someone x).
            if self.commands.is_owner(nick, account) or self.commands.is_public(text):
                await self.commands.handle(nick, target, text, account)
            else:
                log.info("ignoring command from non-owner %s in %s: %s", nick, target, text)
            return
        if not is_channel(target) and not self.cfg.bot.private_messages:
            log.debug("ignoring private message from %s (private_messages is off)", nick)
            return
        self.history.add(key, Entry(nick=nick, text=text, is_action=is_action, account=account))
        if irc_lower(nick) in self.ignore or irc_lower(nick) == irc_lower(self.irc.nick):
            return
        if key in self.muted:
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
            if not any(not e.is_bot and e.seq > conv.answered_seq for e in self.history.entries(key)):
                log.debug("[%s] nothing new since the last reply", target)
                return
            try:
                await self._respond(key, target, conv)
            except Exception:
                log.exception("[%s] failed to respond", target)

    async def _generate(self, messages: list[dict], target: str, conv: Conversation) -> Decision | None:
        raw = await self.llm.chat(messages)
        if raw is None:
            return None
        if not conv.active:
            log.info("[%s] conversation stopped during generation, dropping reply", target)
            return None
        log.debug("[%s] llm raw: %r", target, raw)
        return parse_reply(raw, self.irc.nick, self.cfg.bot.max_reply_chars, self.irc.text_bytes(target))

    def _new_lines(self, key: str, lines: list[str]) -> list[str]:
        """Drop lines repeating one of the bot's recent lines (or an earlier line of the same reply)."""
        n = self.cfg.bot.repeat_check_lines
        if n <= 0:
            return lines
        seen = [ACTION_PREFIX + e.text if e.is_action else e.text for e in self.history.entries(key) if e.is_bot][-n:]
        kept = []
        for line in lines:
            if not is_repeat(line, seen, self.cfg.bot.repeat_similarity):
                kept.append(line)
                seen.append(line)
        return kept

    async def _respond(self, key: str, target: str, conv: Conversation) -> None:
        where = target if is_channel(target) else f"a private chat with {target}"
        members = self.irc.channel_members(target) if is_channel(target) else [target, self.irc.nick]
        system = system_prompt(
            self.instructions.text, self.irc.nick, self.cfg.irc.server, where, self.memory is not None, self.cfg.irc.owner
        )
        entries = self.history.entries(key)
        note = context_note(where, members, self._notes(target, entries), conv.crossed)
        seen_seq = entries[-1].seq if entries else 0
        messages = build_messages(
            system,
            entries,
            self.cfg.llm.context_budget_tokens,
            self.cfg.llm.max_tokens,
            self.history.window(key),
            note,
        )
        decision = await self._generate(messages, target, conv)
        if decision is None:
            return
        conv.answered_seq = max(conv.answered_seq, seen_seq)
        conv.crossed = 0
        if decision.lines:
            # Repeated lines are dropped, not retried; if nothing new is left the bot stays quiet.
            lines = self._new_lines(key, decision.lines)
            if len(lines) < len(decision.lines):
                log.info("[%s] dropped %d repeated line(s)", target, len(decision.lines) - len(lines))
            decision.lines = lines
        if decision.lines:
            # Messages that came in while the reply was being written: they're stored before it (that's
            # when they were sent, before anyone saw the reply), and the next turn is told about them.
            conv.crossed = sum(not e.is_bot and e.seq > seen_seq for e in self.history.entries(key))
        for line in decision.lines:
            if line.startswith(ACTION_PREFIX):
                sent = self.irc.action(target, line[len(ACTION_PREFIX) :])
                entry = Entry(nick=self.irc.nick, text=sent, is_bot=True, is_action=True)
                log.info("[%s] * %s %s", target, self.irc.nick, sent)
            else:
                sent = self.irc.privmsg(target, line)
                entry = Entry(nick=self.irc.nick, text=sent, is_bot=True)
                log.info("[%s] <%s> %s", target, self.irc.nick, sent)
            self.history.add(key, entry)
        if decision.lines:
            conv.touch()
        elif not decision.end:
            log.info("[%s] skipped", target)
        if decision.end:
            log.info("[%s] conversation ended by llm, going idle", target)
            conv.end()

    # ---- long-term memory --------------------------------------------------

    def _notes(self, target: str, entries: list[Entry]) -> str | None:
        """Notes for the prompt: the channel's, and those of the people in the history."""
        if not self.memory:
            return None
        people = speakers(entries, self.cfg.bot.memory_nick_fallback)
        return self.memory.prompt_text(target if is_channel(target) else None, people)

    def _undigested(self, key: str) -> list[Entry]:
        return [e for e in self.history.entries(key) if not e.digested]

    def _digest_due(self, key: str, now: float) -> bool:
        new = self._undigested(key)
        if not is_channel(key) or not any(not e.is_bot for e in new) or now < self._digest_retry.get(key, 0):
            return False
        # Never let undigested lines pile up past what the history keeps (it drops the oldest 10%).
        max_lines = max(1, min(self.cfg.bot.memory_max_lines, self.cfg.bot.context_messages * 3 // 4))
        return now - new[-1].ts >= self.cfg.bot.memory_pause or sum(not e.is_bot for e in new) >= max_lines

    async def _memory_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = time.time()
            for key in [k for k in self.history.keys() if self._digest_due(k, now)]:
                try:
                    await self.digest(key)
                except Exception:
                    log.exception("[%s] memory: summary pass failed", key)

    async def _flush_memory(self) -> None:
        """On quit: summarize whatever hasn't been yet, within the LLM timeout."""
        keys = [k for k in self.history.keys() if is_channel(k) and any(not e.is_bot for e in self._undigested(k))]
        if not keys:
            return
        log.info("memory: summarizing %d channel(s) before quitting", len(keys))
        try:
            async with asyncio.timeout(self.cfg.llm.timeout):
                for key in keys:
                    await self.digest(key)
        except TimeoutError:
            log.warning("memory: summary before quitting took too long, some lines weren't summarized")

    async def digest(self, key: str) -> bool:
        """Summarize a channel's new lines into the long-term notes."""
        if not self.memory:
            return False
        async with self._digest_lock:
            entries = self.history.entries(key)
            new = [e for e in entries if not e.digested]
            if not any(not e.is_bot for e in new):
                return False
            earlier = [e for e in entries if e.digested][-30:]
            d = Digest(self.memory, self.irc.nick, key, new, earlier, self.cfg.bot.memory_nick_fallback)
            log.info("[%s] memory: summarizing %d new lines", key, len(new))
            raw = await self.llm.chat(d.messages(), **self._memory_llm_args())
            if raw is None or not d.apply(raw):
                self._digest_retry[key] = time.time() + 600
                return False
            for e in new:
                e.digested = True
            self._digest_retry.pop(key, None)
            if compact := compact_messages(self.memory, key):
                messages, replaced = compact
                raw = await self.llm.chat(messages, **self._memory_llm_args())
                if raw is not None:
                    apply_compact(self.memory, key, raw, replaced)
            return True

    def _memory_llm_args(self) -> dict:
        b = self.cfg.bot
        return {"temperature": 0.3, "max_tokens": b.memory_max_tokens, "reasoning_effort": b.memory_reasoning_effort}
