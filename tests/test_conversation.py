import asyncio

import pytest

from gmbot.bot import Bot
from gmbot.config import Config
from gmbot.conversation import mentions, parse_reply


def test_mentions():
    assert mentions("hey GMbot what's up", ["GMbot"])
    assert mentions("gmbot: hi", ["GMbot"])
    assert mentions("GMbot, hi", ["GMbot"])
    assert not mentions("GMbot_ is a different nick", ["GMbot"])
    assert not mentions("xgmbot", ["GMbot"])


def test_parse_reply_single_line_and_cleanup():
    d = parse_reply("GMbot: sure thing\n\n  GMbot: second   line\n```bash\n```", "GMbot", 300)
    assert d.lines == ["sure thing", "second line"] and not d.end
    assert parse_reply("<GMbot> yo", "GMbot", 300).text == "yo"


def test_parse_reply_skip_end():
    assert parse_reply("[SKIP]", "GMbot", 300).text is None
    d = parse_reply("[END]", "GMbot", 300)
    assert d.text is None and d.end
    d = parse_reply("np, cya [END]", "GMbot", 300)
    assert d.text == "np, cya" and d.end


def test_parse_reply_wraps_long_lines():
    d = parse_reply("word " * 100, "GMbot", 23)
    assert all(len(ln) <= 23 and not ln.endswith(" ") for ln in d.lines)
    assert " ".join(d.lines) == ("word " * 100).strip()  # nothing lost
    assert parse_reply("x" * 50, "GMbot", 20).lines == ["x" * 20, "x" * 20, "x" * 10]


class FakeIRC:
    nick = "GMbot"

    def __init__(self):
        self.sent = []

    def privmsg(self, target, text):
        self.sent.append((target, text))
        return text

    def channel_members(self, channel):
        return ["alice", "bob", "GMbot"]

    channels = ["#c"]

    def notice(self, target, text):
        self.sent.append(("notice", target, text))

    def action(self, target, text):
        self.sent.append(("action", target, text))
        return text

    def join(self, channel, key=""):
        self.sent.append(("join", channel))

    def part(self, channel, reason=""):
        self.sent.append(("part", channel, reason))


class FakeLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def chat(self, messages):
        self.calls.append(messages)
        return self.replies.pop(0)

    async def aclose(self):
        pass


def make_bot(tmp_path, replies):
    cfg = Config(base_dir=tmp_path)
    cfg.bot.debounce_seconds = 0
    cfg.bot.private_messages = True
    (tmp_path / "INSTRUCTIONS.md").write_text("persona")
    bot = Bot(cfg)
    bot.irc = FakeIRC()
    bot.llm = FakeLLM(replies)
    return bot


async def say(bot, nick, text, target="#c", account=None):
    await bot.on_message(nick, target, text, False, account)
    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_state_machine(tmp_path):
    bot = make_bot(tmp_path, ["hey alice", "probably tuesday", "[SKIP]", "cya [END]"])
    await say(bot, "alice", "random chatter")
    assert bot.llm.calls == []                      # idle: not sent to llm
    await say(bot, "alice", "GMbot hi")
    assert bot.irc.sent == [("#c", "hey alice")]     # mention -> active
    await say(bot, "alice", "when is the release?")
    assert bot.irc.sent[-1] == ("#c", "probably tuesday")  # follow-up without nick
    await say(bot, "bob", "alice: lunch?")
    assert len(bot.irc.sent) == 2                   # skipped but still active
    await say(bot, "alice", "ok thanks bye")
    assert bot.irc.sent[-1] == ("#c", "cya")
    await say(bot, "alice", "more chatter")
    assert len(bot.llm.calls) == 4                  # idle again after [END]
    # full context: the llm saw the pre-mention chatter too
    assert "random chatter" in bot.llm.calls[0][1]["content"]
    assert "persona" in bot.llm.calls[0][0]["content"]


@pytest.mark.asyncio
async def test_timeout_goes_idle(tmp_path):
    bot = make_bot(tmp_path, ["yo"])
    bot.cfg.bot.conversation_timeout = 0
    await say(bot, "alice", "GMbot hi")
    bot.convs["#c"].timeout = 0
    await asyncio.sleep(0.01)
    await say(bot, "alice", "anything")
    assert len(bot.llm.calls) == 1


@pytest.mark.asyncio
async def test_private_message_always_answered(tmp_path):
    bot = make_bot(tmp_path, ["hi there"])
    await say(bot, "alice", "hello", target="alice")
    assert bot.irc.sent == [("alice", "hi there")]


@pytest.mark.asyncio
async def test_ignored_nick(tmp_path):
    bot = make_bot(tmp_path, [])
    await say(bot, "ChanServ", "GMbot something")
    assert bot.llm.calls == []


def test_parse_reply_remember():
    d = parse_reply("cool, noted [REMEMBER: alice runs gentoo] [REMEMBER: bob likes rust]", "GMbot", 300)
    assert d.text == "cool, noted"
    assert d.memories == ["alice runs gentoo", "bob likes rust"]
    d = parse_reply("[REMEMBER: x] [END]", "GMbot", 300)
    assert d.text is None and d.end and d.memories == ["x"]


@pytest.mark.asyncio
async def test_memory_written_and_used(tmp_path):
    bot = make_bot(tmp_path, ["nice [REMEMBER: alice runs gentoo on her laptop]", "gentoo right"])
    await say(bot, "alice", "GMbot fyi i run gentoo on my laptop")
    assert bot.irc.sent[-1] == ("#c", "nice")
    content = (tmp_path / "MEMORY.md").read_text()
    assert "- alice runs gentoo on her laptop (alice, #c, " in content
    await say(bot, "alice", "what do i run?")
    assert "alice runs gentoo on her laptop" in bot.llm.calls[-1][0]["content"]


def test_memory_dedup_and_cap(tmp_path):
    from gmbot.memory import Memory

    m = Memory(tmp_path / "MEMORY.md", max_facts=2)
    assert m.add("a fact", "n", "#c")
    assert not m.add("A fact.", "n", "#c")
    m.add("b", "n", "#c"); m.add("c", "n", "#c")
    assert [ln.split(" (")[0] for ln in m.text.splitlines()] == ["- b", "- c"]
    # manual edits are picked up
    (tmp_path / "MEMORY.md").write_text("- edited by hand\n")
    import os; os.utime(tmp_path / "MEMORY.md", (1, 1))
    assert m.text == "- edited by hand"


def test_memory_disabled_has_no_rules():
    from gmbot.history import system_prompt

    assert "REMEMBER" not in system_prompt("", "GMbot", "net", "#c", [])
    assert "(nothing yet)" in system_prompt("", "GMbot", "net", "#c", [], memory="")


def test_is_repeat():
    from gmbot.conversation import is_repeat

    assert is_repeat("Yeah, that works!", ["yeah that works"], 1.0)
    assert is_repeat("yeah that works fine", ["yeah that work fine"], 0.9)
    assert not is_repeat("yeah that works fine", ["yeah that work fine"], 1.0)
    assert not is_repeat("try the lts kernel", ["yeah that works"], 0.9)


@pytest.mark.asyncio
async def test_repeat_is_ignored_without_retry(tmp_path):
    bot = make_bot(tmp_path, ["use the lts kernel", "use the lts kernel!", "or try 6.12 maybe"])
    await say(bot, "alice", "GMbot which kernel?")
    await say(bot, "alice", "hmm?")
    assert bot.irc.sent == [("#c", "use the lts kernel")]
    assert len(bot.llm.calls) == 2  # no retry
    assert bot.convs["#c"].active
    await say(bot, "alice", "other ideas?")
    assert bot.irc.sent[-1] == ("#c", "or try 6.12 maybe")


@pytest.mark.asyncio
async def test_multiline_reply_sent_line_by_line(tmp_path):
    bot = make_bot(tmp_path, ["first do this\nthen that\nfirst do this"])
    await say(bot, "alice", "GMbot how?")
    # each line is its own message; a line repeating an earlier one in the same reply is dropped
    assert bot.irc.sent == [("#c", "first do this"), ("#c", "then that")]
    assert [e.text for e in bot.history.entries("#c") if e.is_bot] == ["first do this", "then that"]


def test_parse_reply_actions():
    d = parse_reply("/me slaps alice with a trout\nlol\n* GMbot hides", "GMbot", 300)
    assert d.lines == ["/me slaps alice with a trout", "lol", "/me hides"]
    # long actions stay actions when wrapped
    d = parse_reply("/me " + "waves " * 20, "GMbot", 30)
    assert len(d.lines) > 1 and all(ln.startswith("/me ") for ln in d.lines)


@pytest.mark.asyncio
async def test_action_reply_sent_as_ctcp_action(tmp_path):
    bot = make_bot(tmp_path, ["/me slaps alice\nthat's for the bad pun", "ok"])
    await say(bot, "alice", "GMbot knock knock")
    assert bot.irc.sent == [("action", "#c", "slaps alice"), ("#c", "that's for the bad pun")]
    entry = bot.history.entries("#c")[-2]
    assert entry.is_bot and entry.is_action
    await say(bot, "alice", "ouch")
    # the model sees its own action in the same /me form it writes
    assert bot.llm.calls[-1][-2] == {"role": "assistant", "content": "/me slaps alice\nthat's for the bad pun"}


def test_irc_action_framing():
    from gmbot.config import IRCConfig
    from gmbot.irc import IRCClient

    irc = IRCClient(IRCConfig(), "v")
    assert irc.action("#c", "waves" + "x" * 600).startswith("waves")
    line = irc._queue.get_nowait()
    assert line.startswith("PRIVMSG #c :\x01ACTION waves") and line.endswith("\x01")
    assert len(line.encode()) <= 512 - 100


@pytest.mark.asyncio
async def test_repeated_action_is_caught(tmp_path):
    bot = make_bot(tmp_path, ["/me slaps alice", "/me slaps alice", "/me slaps alice"])
    await say(bot, "alice", "GMbot hi")
    await say(bot, "alice", "again")
    assert bot.irc.sent == [("action", "#c", "slaps alice")]


def test_parse_reply_forget():
    d = parse_reply("ok done [FORGET: alice runs gentoo] [REMEMBER: alice runs arch]", "GMbot", 300)
    assert d.lines == ["ok done"] and d.forgets == ["alice runs gentoo"] and d.memories == ["alice runs arch"]


def test_memory_forget_fact(tmp_path):
    from gmbot.memory import Memory

    m = Memory(tmp_path / "MEMORY.md", max_facts=10)
    m.add("alice runs gentoo on her laptop", "alice", "#c")
    m.add("bob likes rust", "bob", "#c")
    # copied with attribution and different punctuation/case: still matched
    assert m.forget_fact("Alice runs Gentoo on her laptop. (alice, #c, 2026-11-10)").startswith("alice runs gentoo")
    # vague or unrelated text removes nothing
    assert m.forget_fact("a") is None and m.forget_fact("everything") is None
    assert [f.split(" (")[0] for f in m.facts] == ["bob likes rust"]


@pytest.mark.asyncio
async def test_forget_then_remember_updates(tmp_path):
    bot = make_bot(tmp_path, ["noted [REMEMBER: alice runs gentoo]", "ah ok [FORGET: alice runs gentoo] [REMEMBER: alice runs arch now]"])
    await say(bot, "alice", "GMbot i run gentoo")
    await say(bot, "alice", "actually i switched to arch")
    assert [f.split(" (")[0] for f in bot.memory.facts] == ["alice runs arch now"]
    assert bot.irc.sent[-1] == ("#c", "ah ok")
