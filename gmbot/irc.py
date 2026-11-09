"""Minimal asyncio IRC client with SASL PLAIN, CTCP, flood control and reconnects."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import ssl
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .config import IRCConfig, default_realname

log = logging.getLogger(__name__)

# Bytes reserved for ":nick!user@host " that the server prepends when relaying our
# messages; the full relayed line must fit in 512 bytes.
PREFIX_RESERVE = 100
MAX_LINE = 512


class SASLError(Exception):
    """SASL authentication failed; retrying with the same credentials is pointless."""


@dataclass
class Message:
    command: str
    params: list[str] = field(default_factory=list)
    prefix: str = ""
    tags: dict[str, str] = field(default_factory=dict)

    @property
    def nick(self) -> str:
        return self.prefix.split("!", 1)[0]


def parse_line(line: str) -> Message:
    tags: dict[str, str] = {}
    prefix = ""
    if line.startswith("@"):
        raw_tags, _, line = line[1:].partition(" ")
        for tag in raw_tags.split(";"):
            k, _, v = tag.partition("=")
            tags[k] = v
        line = line.lstrip(" ")
    if line.startswith(":"):
        prefix, _, line = line[1:].partition(" ")
        line = line.lstrip(" ")
    trailing = None
    if " :" in line:
        line, trailing = line.split(" :", 1)
    elif line.startswith(":"):
        line, trailing = "", line[1:]
    parts = line.split()
    command = parts[0].upper() if parts else ""
    params = parts[1:]
    if trailing is not None:
        params.append(trailing)
    return Message(command=command, params=params, prefix=prefix, tags=tags)


_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ[]\\~", "abcdefghijklmnopqrstuvwxyz{}|^")


def irc_lower(s: str) -> str:
    """rfc1459 casemapping (what Libera uses)."""
    return s.translate(_LOWER)


def sasl_plain_chunks(username: str, password: str) -> list[str]:
    payload = base64.b64encode(f"{username}\0{username}\0{password}".encode()).decode()
    chunks = [payload[i : i + 400] for i in range(0, len(payload), 400)]
    if not chunks or len(chunks[-1]) == 400:
        chunks.append("+")
    return chunks


def truncate_utf8(text: str, max_bytes: int) -> str:
    data = text.encode()
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode(errors="ignore")


def is_channel(target: str) -> bool:
    return target[:1] in "#&+!"


# (nick, target, text, is_action, account): account is the sender's services account, if known.
MessageHandler = Callable[[str, str, str, bool, str | None], Awaitable[None]]


class IRCClient:
    def __init__(self, cfg: IRCConfig, ctcp_version: str):
        self.cfg = cfg
        self.ctcp_version = ctcp_version
        self.nick = cfg.nick
        self.members: dict[str, set[str]] = {}
        self.on_message: MessageHandler | None = None
        self.channels: list[str] = list(cfg.channels)  # joined on (re)connect; .join/.part update it
        self.has_account_tag = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._last_recv = 0.0
        self._registered = False
        self._caps: set[str] = set()
        self._quitting = False
        self._session_done = asyncio.Event()

    # ---- public API -------------------------------------------------------

    async def run(self) -> None:
        backoff = 5.0
        while True:
            try:
                await self._session()
                backoff = 5.0
            except SASLError:
                raise
            except (OSError, asyncio.IncompleteReadError, ConnectionError) as e:
                if not self._quitting:
                    log.warning("connection lost: %s", e)
            if self._quitting:
                log.info("disconnected")
                return
            log.info("reconnecting in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)

    async def quit(self, message: str, timeout: float = 5.0) -> None:
        """Send QUIT and wait for the server to close the connection; stops reconnecting."""
        self._quitting = True
        if self._writer is None or self._writer.is_closing():
            return
        log.info("quitting: %s", message)
        try:
            await self._send_now(f"QUIT :{message}")
            await asyncio.wait_for(self._session_done.wait(), timeout)
        except (OSError, ConnectionError, TimeoutError):
            pass

    def privmsg(self, target: str, text: str) -> str:
        """Queue a PRIVMSG; returns the (possibly truncated) text that will be sent."""
        overhead = len(f"PRIVMSG {target} :".encode()) + PREFIX_RESERVE + 2
        text = truncate_utf8(text.replace("\r", " ").replace("\n", " "), MAX_LINE - overhead)
        self._queue.put_nowait(f"PRIVMSG {target} :{text}")
        return text

    def notice(self, target: str, text: str) -> None:
        overhead = len(f"NOTICE {target} :".encode()) + PREFIX_RESERVE + 2
        text = truncate_utf8(text.replace("\r", " ").replace("\n", " "), MAX_LINE - overhead)
        self._queue.put_nowait(f"NOTICE {target} :{text}")

    def action(self, target: str, text: str) -> None:
        self.privmsg(target, f"\x01ACTION {text}\x01")

    def join(self, channel: str, key: str = "") -> None:
        if irc_lower(channel) not in map(irc_lower, self.channels):
            self.channels.append(channel)
        self._queue.put_nowait(f"JOIN {channel} {key}".rstrip())

    def part(self, channel: str, reason: str = "") -> None:
        self.channels = [c for c in self.channels if irc_lower(c) != irc_lower(channel)]
        self._queue.put_nowait(f"PART {channel} :{reason}" if reason else f"PART {channel}")

    def change_nick(self, nick: str) -> None:
        self._queue.put_nowait(f"NICK {nick}")

    def channel_members(self, channel: str) -> list[str]:
        return sorted(self.members.get(irc_lower(channel), ()), key=str.lower)

    # ---- connection -------------------------------------------------------

    async def _session(self) -> None:
        ctx = ssl.create_default_context() if self.cfg.tls else None
        log.info("connecting to %s:%d (tls=%s)", self.cfg.server, self.cfg.port, self.cfg.tls)
        self._reader, self._writer = await asyncio.open_connection(
            self.cfg.server, self.cfg.port, ssl=ctx, server_hostname=self.cfg.server if ctx else None
        )
        self.nick = self.cfg.nick
        self.members.clear()
        self._registered = False
        self._caps = set()
        self.has_account_tag = False
        self._last_recv = time.monotonic()
        self._queue = asyncio.Queue()
        self._session_done.clear()

        await self._send_now("CAP LS 302")
        await self._send_now(f"NICK {self.nick}")
        realname = self.cfg.realname or default_realname(self.cfg)
        await self._send_now(f"USER {self.cfg.username} 0 * :{realname}")

        tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._send_loop()),
            asyncio.create_task(self._ping_loop()),
        ]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                t.result()  # re-raise
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
            self._session_done.set()

    async def _send_now(self, line: str) -> None:
        assert self._writer is not None
        if line.startswith("AUTHENTICATE ") and line != "AUTHENTICATE PLAIN":
            log.debug(">> AUTHENTICATE ****")
        else:
            log.debug(">> %s", line)
        self._writer.write(truncate_utf8(line, MAX_LINE - 2).encode() + b"\r\n")
        await self._writer.drain()

    async def _send_loop(self) -> None:
        # Token bucket: burst of 4 lines, then one every 1.5s.
        tokens, rate, burst = 4.0, 1 / 1.5, 4.0
        last = time.monotonic()
        while True:
            line = await self._queue.get()
            now = time.monotonic()
            tokens = min(burst, tokens + (now - last) * rate)
            last = now
            if tokens < 1:
                await asyncio.sleep((1 - tokens) / rate)
                tokens, last = 1.0, time.monotonic()
            tokens -= 1
            await self._send_now(line)

    async def _ping_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            idle = time.monotonic() - self._last_recv
            if idle > 240:
                raise ConnectionError("ping timeout")
            if idle > 120:
                await self._send_now(f"PING :{int(time.time())}")

    async def _read_loop(self) -> None:
        assert self._reader is not None
        while True:
            raw = await self._reader.readline()
            if not raw:
                raise ConnectionError("server closed connection")
            self._last_recv = time.monotonic()
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            log.debug("<< %s", line)
            await self._handle(parse_line(line))

    # ---- protocol ---------------------------------------------------------

    async def _handle(self, msg: Message) -> None:
        cmd, p = msg.command, msg.params
        if cmd == "PING":
            await self._send_now(f"PONG :{p[-1] if p else ''}")
        elif cmd == "CAP":
            await self._handle_cap(p)
        elif cmd == "AUTHENTICATE" and p and p[0] == "+":
            for chunk in sasl_plain_chunks(self.cfg.sasl_username, self.cfg.sasl_password):
                await self._send_now(f"AUTHENTICATE {chunk}")
        elif cmd == "903":
            log.info("SASL authentication successful")
            await self._send_now("CAP END")
        elif cmd in ("902", "904", "905", "906", "908"):
            reason = p[-1] if p else cmd
            if self.cfg.sasl_required:
                raise SASLError(f"SASL failed ({cmd}): {reason}")
            log.warning("SASL failed (%s): %s; continuing unauthenticated", cmd, reason)
            await self._send_now("CAP END")
        elif cmd == "001":
            self._registered = True
            self.nick = p[0]
            log.info("registered as %s", self.nick)
            # +B can't be changed while in channels, so it has to go out before the JOINs.
            if self.cfg.bot_mode:
                await self._send_now(f"MODE {self.nick} +B")
            for chan in self.channels:
                await self._send_now(f"JOIN {chan}")
        elif cmd == "433" and not self._registered:
            self.nick += "_"
            await self._send_now(f"NICK {self.nick}")
        elif cmd == "353" and len(p) >= 4:
            names = self.members.setdefault(irc_lower(p[2]), set())
            for name in p[3].split():
                names.add(name.lstrip("~&@%+"))
        elif cmd == "JOIN" and p:
            chan = irc_lower(p[0])
            if irc_lower(msg.nick) == irc_lower(self.nick):
                self.members[chan] = set()
                log.info("joined %s", p[0])
            self.members.setdefault(chan, set()).add(msg.nick)
        elif cmd == "PART" and p:
            self._remove_member(irc_lower(p[0]), msg.nick)
        elif cmd == "KICK" and len(p) >= 2:
            self._remove_member(irc_lower(p[0]), p[1])
        elif cmd == "QUIT":
            for chan in self.members:
                self._discard(self.members[chan], msg.nick)
        elif cmd == "NICK" and p:
            old, new = msg.nick, p[0]
            if irc_lower(old) == irc_lower(self.nick):
                self.nick = new
            for names in self.members.values():
                if self._discard(names, old):
                    names.add(new)
        elif cmd == "PRIVMSG" and len(p) >= 2:
            await self._handle_privmsg(msg.nick, p[0], p[1], msg.tags.get("account"))
        elif cmd == "ERROR":
            raise ConnectionError(f"server error: {p[-1] if p else ''}")

    async def _handle_cap(self, p: list[str]) -> None:
        if len(p) < 3:
            return
        sub = p[1].upper()
        if sub == "LS":
            final = p[2] != "*"
            caps = p[-1]
            self._caps.update(c.split("=", 1)[0] for c in caps.split())
            if not final:
                return
            use_sasl = "sasl" in self._caps and bool(self.cfg.sasl_password)
            if not use_sasl and self.cfg.sasl_required:
                raise SASLError("SASL required but not available or no password configured")
            # account-tag tells us which account sent each message (used to verify the owner).
            want = [c for c in ("account-tag",) if c in self._caps] + (["sasl"] if use_sasl else [])
            if want:
                await self._send_now(f"CAP REQ :{' '.join(want)}")
            else:
                await self._send_now("CAP END")
        elif sub == "ACK":
            acked = p[-1].split()
            if "account-tag" in acked:
                self.has_account_tag = True
            if "sasl" in acked:
                await self._send_now("AUTHENTICATE PLAIN")
            else:
                await self._send_now("CAP END")
        elif sub == "NAK":
            rejected = p[-1].split()
            if "account-tag" in rejected and len(rejected) > 1:
                # The whole request is refused at once; retry with SASL alone.
                self._caps.discard("account-tag")
                await self._send_now("CAP REQ :sasl")
            elif self.cfg.sasl_required:
                raise SASLError("server refused SASL capability")
            else:
                await self._send_now("CAP END")

    async def _handle_privmsg(self, nick: str, target: str, text: str, account: str | None) -> None:
        reply_to = target if is_channel(target) else nick
        is_action = False
        if text.startswith("\x01"):
            body = text.strip("\x01")
            ctcp, _, arg = body.partition(" ")
            ctcp = ctcp.upper()
            if ctcp == "ACTION":
                text, is_action = arg, True
            else:
                if ctcp == "VERSION":
                    self._ctcp_reply(nick, f"VERSION {self.ctcp_version}")
                elif ctcp == "PING":
                    self._ctcp_reply(nick, f"PING {arg}")
                elif ctcp == "TIME":
                    self._ctcp_reply(nick, "TIME " + time.strftime("%a %b %d %H:%M:%S %Y"))
                return
        if self.on_message:
            await self.on_message(nick, reply_to, text, is_action, account)

    def _ctcp_reply(self, nick: str, body: str) -> None:
        self._queue.put_nowait(f"NOTICE {nick} :\x01{body}\x01")

    def _remove_member(self, chan: str, nick: str) -> None:
        if irc_lower(nick) == irc_lower(self.nick):
            self.members.pop(chan, None)
        elif chan in self.members:
            self._discard(self.members[chan], nick)

    @staticmethod
    def _discard(names: set[str], nick: str) -> bool:
        low = irc_lower(nick)
        for n in list(names):
            if irc_lower(n) == low:
                names.discard(n)
                return True
        return False


_FORMATTING = re.compile(r"\x03(\d{1,2}(,\d{1,2})?)?|[\x02\x0f\x11\x16\x1d\x1e\x1f]")


def strip_formatting(text: str) -> str:
    return _FORMATTING.sub("", text)
