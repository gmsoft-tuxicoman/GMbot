from gmbot.history import Entry, History, build_messages, system_prompt


def test_history_cap():
    h = History(3)
    for i in range(5):
        h.add("#c", Entry("a", str(i)))
    assert [e.text for e in h.entries("#c")] == ["2", "3", "4"]


def test_role_mapping_and_merging():
    entries = [
        Entry("GMbot", "earlier bot line", is_bot=True),  # leading bot line is dropped
        Entry("alice", "hi GMbot"),
        Entry("bob", "yo", is_action=True),
        Entry("GMbot", "hey", is_bot=True),
        Entry("alice", "how are you"),
    ]
    msgs = build_messages("SYS", entries, budget_tokens=10000, reply_tokens=100)
    assert msgs == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "<alice> hi GMbot\n* bob yo"},
        {"role": "assistant", "content": "hey"},
        {"role": "user", "content": "<alice> how are you"},
    ]


def test_budget_trims_oldest():
    entries = [Entry("a", "x" * 400) for _ in range(10)] + [Entry("a", "latest")]
    msgs = build_messages("S", entries, budget_tokens=350, reply_tokens=100)
    content = msgs[1]["content"]
    assert content.endswith("<a> latest")
    assert content.count("x" * 400) < 10


def test_system_prompt_contains_instructions_and_rules():
    s = system_prompt("be nice", "GMbot", "irc.libera.chat", "#c", ["alice", "GMbot"])
    assert s.startswith("be nice")
    assert "[SKIP]" in s and "[END]" in s and "alice, GMbot" in s


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
