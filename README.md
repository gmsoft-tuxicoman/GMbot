# GMbot

An IRC bot for [Libera.Chat](https://libera.chat) that chats using a local LLM through any
OpenAI-compatible endpoint (llama.cpp server, vLLM, LM Studio, Ollama's `/v1`, OpenRouter, ...).

It stays quiet until someone mentions it, then joins the conversation like a regular: one short,
casual line at a time, following up without needing to be pinged again, and going quiet once the
conversation no longer involves it.

## Features

- Plain asyncio IRC client: TLS, SASL PLAIN, IRCv3 message parsing, flood control, automatic reconnect
- Libera-friendly: bot mode (+B), owner shown in `/whois`, CTCP `VERSION`/`PING`/`TIME`, clean `QUIT`
- Persona in a Markdown file (`INSTRUCTIONS.md`), reloaded on change without restarting
- The full channel conversation (last 512 messages by default) is used as context
- The LLM decides when to talk, when to stay quiet, and when the conversation is over
- Long-term memory of facts it's told, stored in an editable `MEMORY.md`
- Controls how hard thinking models think (`reasoning_effort`), and strips `<think>` blocks

## Requirements

- Python 3.11+ (uses `tomllib`)
- An OpenAI-compatible chat completions endpoint
- A registered NickServ account for the bot's nick (`/msg NickServ REGISTER <password> <email>`)

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml   # then edit it: nick, owner, channels, base_url, model
```

Secrets can go in `config.toml` (which is git-ignored) or in environment variables:

```sh
export GMBOT_SASL_PASSWORD='nickserv-password'
export GMBOT_LLM_API_KEY='llm-api-key'      # optional for local servers
```

## Running

```sh
.venv/bin/python -m gmbot config.toml       # add -v to log raw IRC traffic and raw LLM output
```

Ctrl+C or `SIGTERM` makes the bot send `QUIT` with `quit_message`, wait up to 5 seconds for the
server to close the connection, and exit. Exit codes: `0` normal, `1` bad config, `2` SASL failure.

### As a systemd service

```ini
# ~/.config/systemd/user/gmbot.service
[Unit]
Description=GMbot IRC bot
After=network-online.target

[Service]
WorkingDirectory=%h/GMbot
ExecStart=%h/GMbot/.venv/bin/python -m gmbot config.toml
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
```

```sh
systemctl --user daemon-reload
systemctl --user enable --now gmbot
journalctl --user -u gmbot -f
```

## How it decides when to talk

Each channel (and private chat) has two states:

- **idle**: messages are stored but nothing is sent to the LLM. When a message mentions the
  bot's nick (or one of `aliases`), it becomes **active**.
- **active**: every new message is sent to the LLM (after `debounce_seconds`, so people can finish
  typing). The model answers with one of:
  - a reply line, sent to the channel
  - `[SKIP]`: say nothing this time but keep following (e.g. others talking among themselves)
  - `[END]`: the conversation is over, go back to idle (a last line before it is allowed: `cya [END]`)

The bot also goes idle after `conversation_timeout` seconds without being mentioned or replying.
Private messages always get an answer.

Whatever the model produces, only the first line is sent, the bot's own nick echoed at the start is
removed, and the line is cut to `max_reply_chars` at a word boundary.

## Persona: INSTRUCTIONS.md

The file set by `instructions_file` (default `INSTRUCTIONS.md`) is the start of the system prompt:
who the bot is, its tone, what it knows. Edit it freely; it's re-read before the next LLM call whenever
its modification time changes (no restart needed). To keep a personal persona outside git, point
`instructions_file` at another file, e.g. `INSTRUCTIONS-GM.md` (git-ignored); changing the config
needs a restart.

The bot appends its own rules after it (`gmbot/history.py`): one short casual line, the `[SKIP]` /
`[END]` / `[REMEMBER]` control words, the current time and the list of people in the channel.

## Memory: MEMORY.md

When someone tells the bot a fact worth knowing later, the model appends `[REMEMBER: the fact]` to
its reply. The bot strips it from the message and adds the fact to `memory_file`:

```
- alice runs gentoo on her laptop (alice, #linuxbe, 2026-11-09)
```

The whole file is included in every prompt. Exact duplicates are ignored, and beyond
`memory_max_facts` the oldest facts are dropped. You can edit or prune it by hand while the bot runs;
it's reloaded on change. Anyone talking to the bot can get things stored, so check it from time to
time. Facts are only learned in active conversations, since idle messages never reach the LLM.

## Configuration

All options with their defaults. See `config.example.toml` for a commented template.

### `[irc]`

| option | default | description |
|---|---|---|
| `server` | `"irc.libera.chat"` | IRC server |
| `port` | `6697` | port |
| `tls` | `true` | use TLS |
| `nick` | `"GMbot"` | nick (`_` is appended if taken) |
| `username` | `"gmbot"` | ident |
| `owner` | `""` | your nick, shown in the realname (a warning is logged if empty) |
| `description` | `"LLM chat bot"` | realname becomes `description \| owner: owner` |
| `realname` | `""` | set to replace the generated realname entirely |
| `bot_mode` | `true` | set user mode +B before joining channels |
| `quit_message` | `"bye for now"` | sent on Ctrl+C / SIGTERM |
| `channels` | `[]` | channels to join |
| `sasl_username` | nick | NickServ account |
| `sasl_password` | `""` | or env `GMBOT_SASL_PASSWORD` |
| `sasl_required` | `true` | exit instead of connecting unauthenticated if SASL fails |

### `[llm]`

| option | default | description |
|---|---|---|
| `base_url` | `"http://localhost:8080/v1"` | OpenAI-compatible endpoint |
| `model` | `"local-model"` | model name (see `GET /v1/models`) |
| `api_key` | `""` | or env `GMBOT_LLM_API_KEY` |
| `temperature` | `0.8` | sampling temperature |
| `max_tokens` | `1024` | max output tokens, thinking included |
| `reasoning_effort` | `"low"` | `"low"`, `"medium"`, `"high"`, or `""` to not send it |
| `context_budget_tokens` | `16000` | approximate prompt size; the oldest messages are dropped beyond it |
| `timeout` | `120` | HTTP timeout in seconds |
| `extra_body` | `{}` | extra fields merged into every request (server-specific options) |

If your server ignores `reasoning_effort`, use `extra_body`, e.g.:

```toml
[llm.extra_body]
chat_template_kwargs = { enable_thinking = false }   # llama.cpp / vLLM, Qwen3-style templates
```

### `[bot]`

| option | default | description |
|---|---|---|
| `instructions_file` | `"INSTRUCTIONS.md"` | persona file |
| `ctcp_version` | `"GMbot 0.1 (Python asyncio IRC bot)"` | CTCP VERSION reply |
| `aliases` | `[]` | other names that wake the bot up |
| `ignore` | `["ChanServ", "NickServ"]` | nicks that never trigger it |
| `context_messages` | `512` | messages kept per channel and sent as context |
| `conversation_timeout` | `600` | seconds without involvement before going idle |
| `debounce_seconds` | `2` | wait for people to finish typing |
| `max_reply_chars` | `300` | maximum reply length |
| `memory_enabled` | `true` | enable `[REMEMBER: ...]` memory |
| `memory_file` | `"MEMORY.md"` | memory file |
| `memory_max_facts` | `200` | maximum number of stored facts |

Relative paths are resolved from the directory containing the config file.

## Tests

```sh
.venv/bin/python -m pytest
```

Unit tests cover IRC parsing and SASL, reply parsing, the conversation state machine (with a fake
LLM), history and prompt building, memory and config loading.

## Project layout

```
gmbot/
  __main__.py       entry point and argument parsing
  config.py         config.toml loading and defaults
  irc.py            IRC client: TLS, SASL, CTCP, flood control, reconnect, QUIT
  llm.py            OpenAI-compatible client
  history.py        message history, system prompt and context building
  conversation.py   idle/active state, parsing [SKIP]/[END]/[REMEMBER]
  memory.py         MEMORY.md handling
  bot.py            ties it all together
tests/              pytest suite
INSTRUCTIONS.md     default persona
config.example.toml config template
```

## Libera.Chat rules for bots

From <https://libera.chat/policies/> and <https://libera.chat/guides/usermodes>:

- Bots must be labeled as bots and attributable to their owner: set `owner`; `bot_mode = true` sets +B.
- Channels are opted out of LLM bots by default: get explicit permission from the channel's operators
  before the bot joins (processing channel content with an LLM counts as public logging). You have to
  ask them yourself; the bot must not ask, and the request must not be written by an LLM.
- LLM bots must be accompanied: you (or another consenting person) must be in every channel the bot is in.
- The owner must be reachable on Libera and is responsible for what the bot says.
- Ask for voice (+v) where possible; it exempts the bot from some spam filters
  (`/msg ChanServ FLAGS #channel GMbot +V`).
