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
    d = parse_reply("GMbot: sure thing\nsecond line", "GMbot", 300)
    assert d.text == "sure thing" and not d.end
    assert parse_reply("<GMbot> yo", "GMbot", 300).text == "yo"


def test_parse_reply_skip_end():
    assert parse_reply("[SKIP]", "GMbot", 300).text is None
    d = parse_reply("[END]", "GMbot", 300)
    assert d.text is None and d.end
    d = parse_reply("np, cya [END]", "GMbot", 300)
    assert d.text == "np, cya" and d.end


def test_parse_reply_truncates_on_word():
    d = parse_reply("word " * 100, "GMbot", 23)
    assert len(d.text) <= 23 and not d.text.endswith(" ")


class FakeIRC:
    nick = "GMbot"

    def __init__(self):
        self.sent = []

    def privmsg(self, target, text):
        self.sent.append((target, text))
        return text

    def channel_members(self, channel):
        return ["alice", "bob", "GMbot"]


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
    (tmp_path / "INSTRUCTIONS.md").write_text("persona")
    bot = Bot(cfg)
    bot.irc = FakeIRC()
    bot.llm = FakeLLM(replies)
    return bot


async def say(bot, nick, text, target="#c"):
    await bot.on_message(nick, target, text, False)
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
    bot._convs["#c"].timeout = 0
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
