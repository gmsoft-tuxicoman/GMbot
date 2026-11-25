from gmbot.history import Entry, History, Window, build_messages, context_note, line_cost, system_prompt


def test_history_cap():
    h = History(3)
    for i in range(5):
        h.add("#c", Entry("a", str(i)))
    assert [e.text for e in h.entries("#c")] == ["2", "3", "4"]


def test_role_mapping_and_merging():
    t = 1_700_000_000  # 2023-11-14 22:13:20 UTC
    entries = [
        Entry("GMbot", "earlier bot line", is_bot=True, ts=t),  # leading bot line is dropped
        Entry("alice", "hi GMbot", ts=t),
        Entry("bob", "yo", is_action=True, ts=t + 60),
        Entry("GMbot", "hey", is_bot=True, ts=t + 61),
        Entry("alice", "how are you", ts=t + 120),
    ]
    msgs = build_messages("SYS", entries, budget_tokens=10000, reply_tokens=100)
    assert msgs == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "[22:13] <alice> hi GMbot\n[22:14] * bob yo"},
        {"role": "assistant", "content": "hey"},
        {"role": "user", "content": "[22:15] <alice> how are you"},
    ]


def test_long_pause_is_marked():
    t = 1_700_000_000
    entries = [
        Entry("alice", "/me slaps GMbot", ts=t),
        Entry("GMbot", "ouch", is_bot=True, ts=t),
        Entry("bob", "morning", ts=t + 4 * 3600 + 12 * 60),
        Entry("carol", "hey", ts=t + 4 * 3600 + 13 * 60),
    ]
    msgs = build_messages("S", entries, budget_tokens=10000, reply_tokens=100)
    assert msgs[-1]["content"] == "--- 4h12 later ---\n[02:25] <bob> morning\n[02:26] <carol> hey"


def test_format_gap():
    from gmbot.history import format_gap

    assert format_gap(45 * 60) == "45min"
    assert format_gap(2 * 3600) == "2h"
    assert format_gap(8 * 3600 + 600) == "8h"
    assert format_gap(3 * 86400) == "3 days"


def test_budget_trims_oldest():
    entries = [Entry("a", "x" * 400) for _ in range(10)] + [Entry("a", "latest")]
    msgs = build_messages("S", entries, budget_tokens=350, reply_tokens=100)
    content = msgs[1]["content"]
    assert content.endswith("<a> latest")
    assert content.count("x" * 400) < 10


def test_system_prompt_contains_instructions_and_rules():
    s = system_prompt("be nice", "GMbot", "irc.libera.chat", "#c", memory=True)
    assert s.startswith("be nice")
    assert "[SKIP]" in s and "[END]" in s


def test_context_note_goes_at_the_end():
    note = context_note("#c", ["alice", "GMbot"], "- a fact")
    assert "people in #c: alice, GMbot" in note and "UTC" in note
    msgs = build_messages("S", [Entry("alice", "hi")], 10000, 100, note=note)
    assert msgs[-1]["content"].endswith("\n\n" + note)
    msgs = build_messages("S", [Entry("alice", "hi"), Entry("GMbot", "yo", is_bot=True)], 10000, 100, note=note)
    assert msgs[-1] == {"role": "user", "content": note}


def test_window_culls_in_chunks_and_stays_stable():
    h = History(10000)
    w = Window()
    for i in range(100):
        h.add("#c", Entry("a", f"{i:03d} " + "x" * 36))
    cost = line_cost(h.entries("#c")[0])
    budget = 100 * cost + 100 + 4  # exactly fits all 100 lines (system "S" = 4 tokens)
    first = build_messages("S", h.entries("#c"), budget, 100, w)[1]["content"].splitlines()[0]
    assert first.endswith("000 " + "x" * 36)
    h.add("#c", Entry("a", "100 " + "x" * 36))
    msgs = build_messages("S", h.entries("#c"), budget, 100, w)
    lines = msgs[1]["content"].splitlines()
    assert lines[0].endswith("010 " + "x" * 36) and len(lines) == 91  # 10% dropped at once
    for i in range(101, 110):  # the next 9 lines fit without moving the start
        h.add("#c", Entry("a", f"{i} " + "x" * 36))
        lines = build_messages("S", h.entries("#c"), budget, 100, w)[1]["content"].splitlines()
        assert lines[0].endswith("010 " + "x" * 36)


def test_history_culls_in_chunks():
    h = History(100)
    for i in range(101):
        h.add("#c", Entry("a", str(i)))
    assert [e.text for e in h.entries("#c")][0] == "10" and len(h.entries("#c")) == 91


def test_llm_request_body_includes_reasoning_effort():
    import asyncio, httpx
    from gmbot.config import LLMConfig
    from gmbot.llm import LLMClient

    seen = {}

    def handler(request):
        import json
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "<think>x</think>ok"}}]})

    cfg = LLMConfig(extra_body={"chat_template_kwargs": {"enable_thinking": False}})
    client = LLMClient(cfg)
    client._http = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    assert asyncio.run(client.chat([{"role": "user", "content": "hi"}])) == "ok"
    assert seen["reasoning_effort"] == "low"
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}

    seen.clear()
    client.cfg.reasoning_effort = ""
    asyncio.run(client.chat([]))
    assert "reasoning_effort" not in seen


def test_llm_logs_thinking(caplog):
    import asyncio, logging, httpx
    from gmbot.config import LLMConfig
    from gmbot.llm import LLMClient

    responses = [
        {"content": "ok", "reasoning_content": "he asks about $85"},
        {"content": "<think>inline thoughts</think>ok"},
    ]

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": responses.pop(0)}]})

    client = LLMClient(LLMConfig())
    client._http = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    with caplog.at_level(logging.DEBUG, logger="gmbot.llm"):
        assert asyncio.run(client.chat([])) == "ok"
        assert asyncio.run(client.chat([])) == "ok"
    assert "llm thinking: he asks about $85" in caplog.text
    assert "llm thinking: inline thoughts" in caplog.text


def test_llm_timeout_is_not_retried(caplog):
    import asyncio, httpx
    from gmbot.config import LLMConfig
    from gmbot.llm import LLMClient

    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("", request=request)

    client = LLMClient(LLMConfig())
    client._http = httpx.AsyncClient(base_url="http://x/v1", transport=httpx.MockTransport(handler))
    assert asyncio.run(client.chat([])) is None
    assert len(calls) == 1
    assert "timed out after 120s (ReadTimeout)" in caplog.text


def test_split_think_handles_lone_closing_tag():
    from gmbot.llm import split_think

    assert split_think("<think>hmm</think>\n\nsalut") == ("hmm", "salut")
    # Qwen-style templates put the opening <think> in the prompt: only the closing tag comes back
    assert split_think("je réfléchis</think>\n\nsalut") == ("je réfléchis", "salut")
    assert split_think("</think>\n\nsalut") == ("", "salut")
    assert split_think("<think>unfinished") == ("unfinished", "")
    assert split_think("just an answer") == ("", "just an answer")
