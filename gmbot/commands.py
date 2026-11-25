"""Owner commands (.join, .part, .thinking, ...), handled in Python and never sent to the LLM."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from .irc import irc_lower, is_channel

if TYPE_CHECKING:
    from .bot import Bot

log = logging.getLogger(__name__)

THINKING_LEVELS = {"off": "", "none": "", "low": "low", "mid": "medium", "medium": "medium", "high": "high"}


@dataclass
class Context:
    nick: str
    target: str  # channel the command was typed in, or the owner's nick for a private message
    account: str | None = None

    @property
    def channel(self) -> str | None:
        return self.target if is_channel(self.target) else None


Handler = Callable[["Commands", Context, list[str]], Awaitable[None]]
_COMMANDS: dict[str, tuple[Handler, str]] = {}
_PUBLIC: set[str] = set()  # commands anyone can run, not just the owner


def command(name: str, usage: str, public: bool = False):
    def register(fn: Handler) -> Handler:
        _COMMANDS[name] = (fn, usage)
        if public:
            _PUBLIC.add(name)
        return fn

    return register


class Commands:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.prefix = bot.cfg.bot.command_prefix
        owner = bot.cfg.irc.owner
        self.owner_nick = irc_lower(owner)
        self.owner_account = irc_lower(bot.cfg.bot.owner_account or owner)
        self._nick_requester: Context | None = None
        bot.irc.on_nick_result = self._nick_result

    def is_owner(self, nick: str, account: str | None) -> bool:
        if not self.owner_nick or irc_lower(nick) != self.owner_nick:
            return False
        if not self.bot.cfg.bot.owner_require_account:
            return True
        return account is not None and irc_lower(account) == self.owner_account

    def _name(self, text: str) -> str:
        return text[len(self.prefix) :].split(" ", 1)[0].lower()

    def is_command(self, text: str) -> bool:
        return text.startswith(self.prefix) and self._name(text) in _COMMANDS

    def is_public(self, text: str) -> bool:
        return self._name(text) in _PUBLIC

    async def handle(self, nick: str, target: str, text: str, account: str | None = None) -> None:
        name, *args = text[len(self.prefix) :].split()
        fn, _ = _COMMANDS[name.lower()]
        log.info("command from %s in %s: %s", nick, target, text)
        ctx = Context(nick, target, account)
        try:
            await fn(self, ctx, args)
        except Exception as e:
            log.exception("command %s failed", name)
            self.reply(ctx, f"error: {e}")

    def reply(self, ctx: Context, text: str) -> None:
        self.bot.irc.notice(ctx.nick, text)

    def _channel_arg(self, ctx: Context, args: list[str]) -> str | None:
        """First argument if it's a channel, else the channel the command was typed in."""
        if args and is_channel(args[0]):
            return args.pop(0)
        return ctx.channel

    # ---- commands ---------------------------------------------------------

    @command("help", "help - list commands")
    async def _help(self, ctx: Context, args: list[str]) -> None:
        for _, usage in _COMMANDS.values():
            self.reply(ctx, self.prefix + usage)

    @command("join", "join #channel [key] - join a channel (until restart)")
    async def _join(self, ctx: Context, args: list[str]) -> None:
        if not args or not is_channel(args[0]):
            return self.reply(ctx, f"usage: {self.prefix}join #channel [key]")
        self.bot.irc.join(args[0], args[1] if len(args) > 1 else "")
        self.reply(ctx, f"joining {args[0]}")

    @command("part", "part [#channel] [reason] - leave a channel (default: this one)")
    async def _part(self, ctx: Context, args: list[str]) -> None:
        chan = self._channel_arg(ctx, args)
        if not chan:
            return self.reply(ctx, f"usage: {self.prefix}part #channel [reason]")
        self.bot.irc.part(chan, " ".join(args))
        self.bot.reset(irc_lower(chan))
        self.reply(ctx, f"leaving {chan}")

    @command("quit", "quit [message] - disconnect and stop the bot")
    async def _quit(self, ctx: Context, args: list[str]) -> None:
        self.bot.request_quit(" ".join(args) or self.bot.cfg.irc.quit_message)

    @command("nick", "nick newnick - change nick")
    async def _nick(self, ctx: Context, args: list[str]) -> None:
        if len(args) != 1:
            return self.reply(ctx, f"usage: {self.prefix}nick newnick")
        self._nick_requester = ctx
        self.bot.irc.change_nick(args[0])

    def _nick_result(self, ok: bool, nick: str, reason: str) -> None:
        ctx, self._nick_requester = self._nick_requester, None
        if ctx:
            self.reply(ctx, f"nick is now {nick}" if ok else f"nick change failed ({reason}), still {nick}")

    @command("say", "say [#channel|nick] text - make the bot say something")
    async def _say(self, ctx: Context, args: list[str]) -> None:
        await self._speak(ctx, args, action=False)

    @command("act", "act [#channel|nick] text - make the bot do a /me")
    async def _act(self, ctx: Context, args: list[str]) -> None:
        await self._speak(ctx, args, action=True)

    async def _speak(self, ctx: Context, args: list[str], action: bool) -> None:
        if ctx.channel and not (args and is_channel(args[0])):
            target = ctx.channel
        elif len(args) >= 2:
            target = args.pop(0)
        else:
            return self.reply(ctx, f"usage: {self.prefix}{'act' if action else 'say'} #channel|nick text")
        if not args:
            return self.reply(ctx, "nothing to say")
        self.bot.say(target, " ".join(args), action=action)

    @command("thinking", "thinking [off|low|mid|high] - show or set how hard the model thinks")
    async def _thinking(self, ctx: Context, args: list[str]) -> None:
        llm = self.bot.cfg.llm
        if args:
            level = args[0].lower()
            if level not in THINKING_LEVELS:
                return self.reply(ctx, "usage: thinking off|low|mid|high")
            llm.reasoning_effort = THINKING_LEVELS[level]
        self.reply(ctx, f"thinking: {llm.reasoning_effort or 'off (not sent)'}")

    @command("pm", "pm [on|off] - show or set whether the bot answers private messages")
    async def _pm(self, ctx: Context, args: list[str]) -> None:
        if args:
            value = args[0].lower()
            if value not in ("on", "off"):
                return self.reply(ctx, f"usage: {self.prefix}pm on|off")
            self.bot.cfg.bot.private_messages = value == "on"
        self.reply(ctx, f"private messages: {'on' if self.bot.cfg.bot.private_messages else 'off'}")

    @command("model", "model [name] - show or set the LLM model")
    async def _model(self, ctx: Context, args: list[str]) -> None:
        if args:
            self.bot.cfg.llm.model = args[0]
        self.reply(ctx, f"model: {self.bot.cfg.llm.model}")

    @command("mute", "mute [#channel] - never answer in a channel (until unmute/restart)")
    async def _mute(self, ctx: Context, args: list[str]) -> None:
        chan = self._channel_arg(ctx, args)
        if not chan:
            return self.reply(ctx, f"usage: {self.prefix}mute #channel")
        self.bot.muted.add(irc_lower(chan))
        self.bot.reset(irc_lower(chan), clear_history=False)
        self.reply(ctx, f"muted in {chan}")

    @command("unmute", "unmute [#channel] - allow answering in a channel again")
    async def _unmute(self, ctx: Context, args: list[str]) -> None:
        chan = self._channel_arg(ctx, args)
        if not chan:
            return self.reply(ctx, f"usage: {self.prefix}unmute #channel")
        self.bot.muted.discard(irc_lower(chan))
        self.reply(ctx, f"unmuted in {chan}")

    @command("stop", "stop [#channel] - end the current conversation and go idle (history is kept)")
    async def _stop(self, ctx: Context, args: list[str]) -> None:
        target = self._channel_arg(ctx, args) or ctx.nick
        self.bot.reset(irc_lower(target), clear_history=False)
        self.reply(ctx, f"idle in {target}")

    @command("reset", "reset [#channel] - forget the channel's conversation history and go idle")
    async def _reset(self, ctx: Context, args: list[str]) -> None:
        target = self._channel_arg(ctx, args) or ctx.nick
        self.bot.reset(irc_lower(target))
        self.reply(ctx, f"history cleared for {target}")

    @command("forget", "forget #channel|account [text] - remove notes containing text (all of them without text)")
    async def _forget(self, ctx: Context, args: list[str]) -> None:
        if not self.bot.memory:
            return self.reply(ctx, "memory is disabled")
        if not args:
            return self.reply(ctx, f"usage: {self.prefix}forget #channel|account [text]")
        notes = self.bot.memory.target(args[0])
        if len(args) == 1:
            return self.reply(ctx, f"deleted the notes about {args[0]}" if notes.delete() else f"no notes about {args[0]}")
        n = notes.remove_matching(" ".join(args[1:]))
        self.reply(ctx, f"removed {n} line{'s' if n != 1 else ''} from the notes about {args[0]}")

    @command("forgetme", "forgetme - make the bot forget everything it noted about you (anyone can use it)", public=True)
    async def _forgetme(self, ctx: Context, args: list[str]) -> None:
        if not self.bot.memory:
            return self.reply(ctx, "i don't keep notes about anyone")
        who = ctx.account or (ctx.nick if self.bot.cfg.bot.memory_nick_fallback else None)
        if not who:
            return self.reply(ctx, "notes are kept per NickServ account, and you're not identified, so there's nothing about you")
        deleted = self.bot.memory.person(who).delete()
        log.info("%s (account %s) asked to be forgotten: %s", ctx.nick, ctx.account, "done" if deleted else "no notes")
        self.reply(ctx, "done, i forgot everything i noted about you" if deleted else "i had no notes about you")

    @command("memory", "memory [#channel|account] - show the notes about a channel (default: this one) or a person")
    async def _memory(self, ctx: Context, args: list[str]) -> None:
        if not self.bot.memory:
            return self.reply(ctx, "memory is disabled")
        name = args[0] if args else ctx.channel
        if not name:
            people, channels = self.bot.memory.counts()
            return self.reply(ctx, f"notes about {people} people and {channels} channels; usage: {self.prefix}memory #channel|account")
        notes = self.bot.memory.target(name)
        sections = [(sec, notes.get(sec)) for sec in ("about", "journal", "facts")]
        if not any(lines for _, lines in sections):
            return self.reply(ctx, f"no notes about {name}")
        for sec, lines in sections:
            if lines:
                shown = lines[-10:]
                more = f" (last {len(shown)} of {len(lines)})" if len(lines) > len(shown) else ""
                self.reply(ctx, f"{name} {sec}{more}:")
                for line in shown:
                    self.reply(ctx, f"- {line}")

    @command("digest", "digest [#channel] - summarize the channel's new lines into the notes now")
    async def _digest(self, ctx: Context, args: list[str]) -> None:
        chan = self._channel_arg(ctx, args)
        if not chan:
            return self.reply(ctx, f"usage: {self.prefix}digest #channel")
        if not self.bot.memory:
            return self.reply(ctx, "memory is disabled")
        self.reply(ctx, f"summarizing {chan}...")
        done = await self.bot.digest(irc_lower(chan))
        self.reply(ctx, "notes updated" if done else "nothing new to summarize (or the summary failed, see the log)")

    @command("status", "status - show channels, conversations and settings")
    async def _status(self, ctx: Context, args: list[str]) -> None:
        bot, llm = self.bot, self.bot.cfg.llm
        active = [k for k, c in bot.convs.items() if c.active]
        self.reply(ctx, f"nick {bot.irc.nick}, channels: {', '.join(bot.irc.channels) or 'none'}")
        pm = "on" if bot.cfg.bot.private_messages else "off"
        self.reply(ctx, f"model {llm.model}, thinking {llm.reasoning_effort or 'off'}, private messages {pm}")
        self.reply(ctx, f"active: {', '.join(active) or 'none'}; muted: {', '.join(sorted(bot.muted)) or 'none'}")
        if bot.memory:
            people, channels = bot.memory.counts()
            self.reply(ctx, f"memory: notes about {people} people and {channels} channels")
