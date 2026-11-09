"""Configuration loading: config.toml plus environment overrides for secrets."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class IRCConfig:
    server: str = "irc.libera.chat"
    port: int = 6697
    tls: bool = True
    nick: str = "GMbot"
    username: str = "gmbot"
    # Libera requires bots to be labeled as bots and attributable to their owner; by default the
    # realname (gecos, shown in /whois) is built from description + owner.
    owner: str = ""
    description: str = "LLM chat bot"
    realname: str = ""
    bot_mode: bool = True  # set user mode +B before joining channels
    quit_message: str = "bye for now"
    channels: list[str] = field(default_factory=list)
    sasl_username: str = ""
    sasl_password: str = ""
    sasl_required: bool = True


@dataclass
class LLMConfig:
    base_url: str = "http://localhost:8080/v1"
    model: str = "local-model"
    api_key: str = ""
    temperature: float = 0.8
    max_tokens: int = 1024
    reasoning_effort: str = "low"
    extra_body: dict = field(default_factory=dict)
    context_budget_tokens: int = 16000
    timeout: float = 120.0


@dataclass
class BotConfig:
    instructions_file: str = "INSTRUCTIONS.md"
    ctcp_version: str = "GMbot 0.1 (Python asyncio IRC bot)"
    aliases: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=lambda: ["ChanServ", "NickServ"])
    context_messages: int = 512
    conversation_timeout: float = 600.0
    debounce_seconds: float = 2.0
    max_reply_chars: int = 300
    private_messages: bool = False  # answer private messages (owner commands always work)
    repeat_check_lines: int = 5  # compare each reply with the bot's last N lines here (0 = off)
    repeat_similarity: float = 0.9  # 1.0 = exact repeats only (ignoring case/punctuation)
    command_prefix: str = "."
    owner_account: str = ""  # services account allowed to run commands; defaults to irc.owner
    owner_require_account: bool = True  # also check the account, not just the (spoofable) nick
    memory_enabled: bool = True
    memory_file: str = "MEMORY.md"
    memory_max_facts: int = 200


@dataclass
class Config:
    irc: IRCConfig = field(default_factory=IRCConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    bot: BotConfig = field(default_factory=BotConfig)
    base_dir: Path = field(default_factory=Path.cwd)

    def _path(self, name: str) -> Path:
        p = Path(name)
        return p if p.is_absolute() else self.base_dir / p

    @property
    def instructions_path(self) -> Path:
        return self._path(self.bot.instructions_file)

    @property
    def memory_path(self) -> Path:
        return self._path(self.bot.memory_file)


def _fill(cls, data: dict):
    known = cls.__dataclass_fields__
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {', '.join(sorted(unknown))}")
    return cls(**data)


def load_config(path: str | os.PathLike) -> Config:
    path = Path(path)
    with path.open("rb") as f:
        data = tomllib.load(f)
    cfg = Config(
        irc=_fill(IRCConfig, data.get("irc", {})),
        llm=_fill(LLMConfig, data.get("llm", {})),
        bot=_fill(BotConfig, data.get("bot", {})),
        base_dir=path.resolve().parent,
    )
    if pw := os.environ.get("GMBOT_SASL_PASSWORD"):
        cfg.irc.sasl_password = pw
    if key := os.environ.get("GMBOT_LLM_API_KEY"):
        cfg.llm.api_key = key
    if not cfg.irc.sasl_username:
        cfg.irc.sasl_username = cfg.irc.nick
    if not cfg.irc.realname:
        cfg.irc.realname = default_realname(cfg.irc)
    return cfg


def default_realname(irc: IRCConfig) -> str:
    if irc.owner:
        return f"{irc.description} | owner: {irc.owner}"
    return irc.description
