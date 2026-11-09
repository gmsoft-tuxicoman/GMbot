import pytest

from test_conversation import make_bot, say


def owner_bot(tmp_path, replies=()):
    bot = make_bot(tmp_path, list(replies))
    bot.cfg.irc.owner = "GMsoft"
    from gmbot.commands import Commands

    bot.commands = Commands(bot)
    return bot


@pytest.mark.asyncio
async def test_owner_command_not_sent_to_llm_or_history(tmp_path):
    bot = owner_bot(tmp_path)
    await say(bot, "GMsoft", ".join #new", account="GMsoft")
    assert ("join", "#new") in bot.irc.sent
    assert bot.llm.calls == [] and bot.history.entries("#c") == []


@pytest.mark.asyncio
async def test_spoofed_owner_nick_rejected(tmp_path):
    bot = owner_bot(tmp_path)
    await say(bot, "GMsoft", ".join #evil", account=None)
    await say(bot, "GMsoft", ".join #evil", account="someoneelse")
    assert not any(m[0] == "join" for m in bot.irc.sent)
    # treated as normal chat instead
    assert len(bot.history.entries("#c")) == 2


@pytest.mark.asyncio
async def test_account_check_can_be_disabled(tmp_path):
    bot = owner_bot(tmp_path)
    bot.cfg.bot.owner_require_account = False
    await say(bot, "gmsoft", ".part", account=None)
    assert ("part", "#c", "") in bot.irc.sent


@pytest.mark.asyncio
async def test_non_owner_and_unknown_commands_are_chat(tmp_path):
    bot = owner_bot(tmp_path, ["sure"])
    await say(bot, "GMsoft", ".notacommand GMbot", account="GMsoft")
    assert bot.irc.sent[-1] == ("#c", "sure")


@pytest.mark.asyncio
async def test_thinking_and_model(tmp_path):
    bot = owner_bot(tmp_path)
    await say(bot, "GMsoft", ".thinking high", target="GMbot", account="GMsoft")
    assert bot.cfg.llm.reasoning_effort == "high"
    await say(bot, "GMsoft", ".thinking mid", account="GMsoft")
    assert bot.cfg.llm.reasoning_effort == "medium"
    await say(bot, "GMsoft", ".thinking off", account="GMsoft")
    assert bot.cfg.llm.reasoning_effort == ""
    await say(bot, "GMsoft", ".model other", account="GMsoft")
    assert bot.cfg.llm.model == "other"
    assert bot.irc.sent[-1] == ("notice", "GMsoft", "model: other")


@pytest.mark.asyncio
async def test_mute_and_reset(tmp_path):
    bot = owner_bot(tmp_path, ["hi"])
    await say(bot, "GMsoft", ".mute", account="GMsoft")
    await say(bot, "alice", "GMbot hello")
    assert bot.llm.calls == []
    await say(bot, "GMsoft", ".unmute", account="GMsoft")
    await say(bot, "GMsoft", ".reset", account="GMsoft")
    assert bot.history.entries("#c") == []
    await say(bot, "alice", "GMbot hello")
    assert bot.irc.sent[-1] == ("#c", "hi")


@pytest.mark.asyncio
async def test_say_act_quit(tmp_path):
    bot = owner_bot(tmp_path)
    await say(bot, "GMsoft", ".say #other hello there", target="GMbot", account="GMsoft")
    assert bot.irc.sent[-1] == ("#other", "hello there")
    assert bot.history.entries("#other")[-1].is_bot
    await say(bot, "GMsoft", ".act waves", account="GMsoft")
    assert bot.irc.sent[-1] == ("action", "#c", "waves")
    await say(bot, "GMsoft", ".quit see you", account="GMsoft")
    assert bot._stop.is_set() and bot._quit_message == "see you"


@pytest.mark.asyncio
async def test_memory_commands(tmp_path):
    bot = owner_bot(tmp_path)
    await say(bot, "GMsoft", ".remember bob uses arch", account="GMsoft")
    await say(bot, "GMsoft", ".remember alice uses gentoo", account="GMsoft")
    assert len(bot.memory.facts) == 2
    await say(bot, "GMsoft", ".forget bob", account="GMsoft")
    assert [f.split(" (")[0] for f in bot.memory.facts] == ["alice uses gentoo"]
    await say(bot, "GMsoft", ".memory", account="GMsoft")
    assert bot.irc.sent[-2] == ("notice", "GMsoft", "1 fact in MEMORY.md")


@pytest.mark.asyncio
async def test_stop_goes_idle_keeps_history(tmp_path):
    bot = owner_bot(tmp_path, ["hey", "should not be called"])
    await say(bot, "alice", "GMbot hi")
    assert bot.convs["#c"].active
    await say(bot, "GMsoft", ".stop", account="GMsoft")
    assert not bot.convs["#c"].active
    await say(bot, "alice", "still there?")
    assert len(bot.llm.calls) == 1
    assert len(bot.history.entries("#c")) == 3


@pytest.mark.asyncio
async def test_stop_during_generation_drops_reply(tmp_path):
    import asyncio

    bot = owner_bot(tmp_path)
    gate = asyncio.Event()

    async def slow_chat(messages):
        await gate.wait()
        return "late reply"

    bot.llm.chat = slow_chat
    await say(bot, "alice", "GMbot hi")
    await say(bot, "GMsoft", ".stop", account="GMsoft")
    gate.set()
    await asyncio.sleep(0.01)
    assert ("#c", "late reply") not in bot.irc.sent


@pytest.mark.asyncio
async def test_private_messages_toggle(tmp_path):
    bot = owner_bot(tmp_path, ["hi alice"])
    bot.cfg.bot.private_messages = False
    await say(bot, "alice", "hello", target="alice")
    assert bot.llm.calls == [] and bot.history.entries("alice") == []
    # owner commands still work in private
    await say(bot, "GMsoft", ".pm on", target="GMsoft", account="GMsoft")
    assert bot.irc.sent[-1] == ("notice", "GMsoft", "private messages: on")
    await say(bot, "alice", "hello", target="alice")
    assert bot.irc.sent[-1] == ("alice", "hi alice")
