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

    @property
    def channel(self) -> str | None:
        return self.target if is_channel(self.target) else None


Handler = Callable[["Commands", Context, list[str]], Awaitable[None]]
_COMMANDS: dict[str, tuple[Handler, str]] = {}


def command(name: str, usage: str):
    def register(fn: Handler) -> Handler:
        _COMMANDS[name] = (fn, usage)
        return fn

    return register


class Commands:
    def __init__(self, bot: Bot):
        self.bot = bot
        self.prefix = bot.cfg.bot.command_prefix
        owner = bot.cfg.irc.owner
        self.owner_nick = irc_lower(owner)
        self.owner_account = irc_lower(bot.cfg.bot.owner_account or owner)

    def is_owner(self, nick: str, account: str | None) -> bool:
        if not self.owner_nick or irc_lower(nick) != self.owner_nick:
            return False
        if not self.bot.cfg.bot.owner_require_account:
            return True
        return account is not None and irc_lower(account) == self.owner_account

    def is_command(self, text: str) -> bool:
        name = text[len(self.prefix) :].split(" ", 1)[0].lower()
        return text.startswith(self.prefix) and name in _COMMANDS

    async def handle(self, nick: str, target: str, text: str) -> None:
        name, *args = text[len(self.prefix) :].split()
        fn, _ = _COMMANDS[name.lower()]
        log.info("owner command from %s in %s: %s", nick, target, text)
        try:
            await fn(self, Context(nick, target), args)
        except Exception as e:
            log.exception("command %s failed", name)
            self.reply(Context(nick, target), f"error: {e}")

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
        self.bot.irc.change_nick(args[0])

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

    @command("remember", "remember fact - add a fact to long-term memory")
    async def _remember(self, ctx: Context, args: list[str]) -> None:
        if not self.bot.memory:
            return self.reply(ctx, "memory is disabled")
        if not args:
            return self.reply(ctx, f"usage: {self.prefix}remember fact")
        added = self.bot.memory.add(" ".join(args), ctx.nick, "manual")
        self.reply(ctx, "remembered" if added else "already known")

    @command("forget", "forget text - remove memory facts containing text")
    async def _forget(self, ctx: Context, args: list[str]) -> None:
        if not self.bot.memory:
            return self.reply(ctx, "memory is disabled")
        if not args:
            return self.reply(ctx, f"usage: {self.prefix}forget text")
        n = self.bot.memory.forget(" ".join(args))
        self.reply(ctx, f"forgot {n} fact{'s' if n != 1 else ''}")

    @command("memory", "memory [n] - show the last n remembered facts (default 5)")
    async def _memory(self, ctx: Context, args: list[str]) -> None:
        if not self.bot.memory:
            return self.reply(ctx, "memory is disabled")
        facts = self.bot.memory.facts
        n = int(args[0]) if args and args[0].isdigit() else 5
        self.reply(ctx, f"{len(facts)} fact{'s' if len(facts) != 1 else ''} in {self.bot.memory.path.name}")
        for fact in facts[-n:] if n else []:
            self.reply(ctx, fact)

    @command("status", "status - show channels, conversations and settings")
    async def _status(self, ctx: Context, args: list[str]) -> None:
        bot, llm = self.bot, self.bot.cfg.llm
        active = [k for k, c in bot.convs.items() if c.active]
        self.reply(ctx, f"nick {bot.irc.nick}, channels: {', '.join(bot.irc.channels) or 'none'}")
        pm = "on" if bot.cfg.bot.private_messages else "off"
        self.reply(ctx, f"model {llm.model}, thinking {llm.reasoning_effort or 'off'}, private messages {pm}")
        self.reply(ctx, f"active: {', '.join(active) or 'none'}; muted: {', '.join(sorted(bot.muted)) or 'none'}")
        if bot.memory:
            self.reply(ctx, f"memory: {len(bot.memory.facts)} facts")
