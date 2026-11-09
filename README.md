# GMbot

IRC bot for Libera.Chat backed by any OpenAI-compatible LLM endpoint (llama.cpp server, vLLM, LM Studio, Ollama's `/v1`, ...).

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.toml config.toml   # edit nick, channels, base_url, model
export GMBOT_SASL_PASSWORD='your-nickserv-password'
.venv/bin/python -m gmbot config.toml        # -v for raw IRC traffic + LLM output
```

The nick must be a registered NickServ account (`/msg NickServ REGISTER ...`); SASL PLAIN over TLS is used to log in.
With `sasl_required = true` the bot exits if authentication fails instead of connecting unauthenticated.

## How it behaves

- Every message in joined channels is kept (last `context_messages`, default 512) and sent as context.
- **idle**: the bot stays silent and nothing goes to the LLM until its nick (or an `aliases` entry) is mentioned.
- **active**: after a mention, every new message goes to the LLM, which answers with one short line,
  `[SKIP]` (stay quiet this time, keep following) or `[END]` (conversation over, back to idle).
- It also goes idle after `conversation_timeout` seconds without being mentioned or replying.
- Private messages are always answered.
- Ctrl+C or SIGTERM sends `QUIT` with `quit_message` and waits (up to 5s) for the server to close the link.
- Memory: when told a fact worth keeping, the LLM appends `[REMEMBER: ...]` to its reply; the bot strips it
  and appends the fact (with who said it, where and when) to `MEMORY.md`, which is included in every prompt.
  Edit or prune the file by hand anytime; it is reloaded on change. Disable with `memory_enabled = false`.
- Replies are forced to a single line of at most `max_reply_chars`.
- `reasoning_effort` (default `low`) controls how hard thinking models think; `[llm.extra_body]` passes
  server-specific options (see `config.example.toml`). `<think>` blocks are stripped from replies.

The persona lives in `INSTRUCTIONS.md` and is reloaded automatically when edited. The IRC/protocol rules
(one line, casual style, `[SKIP]`/`[END]`) are appended by the bot in `gmbot/history.py`.

## Tests

```sh
.venv/bin/python -m pytest
```

## Libera.Chat policy checklist

From <https://libera.chat/policies/> and <https://libera.chat/guides/usermodes>:

- Bots must be labeled as bots and attributable to their owner: set `owner` (realname/whois shows
  `description | owner: ...`); `bot_mode = true` sets +B before joining channels.
- Channels are opted out of LLM bots by default: get explicit permission from the channel's ops
  before the bot joins (it processes channel content, which counts as public logging).
- LLM bots must be accompanied: you (or a consenting human) must be in every channel the bot is in.
- The owner must be reachable on Libera and is responsible for what the bot says.
- Ask for voice (+v) in channels where possible; it exempts the bot from some spam filters.
