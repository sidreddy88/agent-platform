"""LLMService's Anthropic client: bounded per-attempt timeout, one retry layer."""
def test_llm_client_has_bounded_timeout_and_a_single_retry_layer():
    from app.services.llm import LLMService

    client = LLMService()._client
    assert client.max_retries == 0
    assert client.timeout.read == 300.0 and client.timeout.connect == 10.0


def test_complete_returns_the_text_after_a_leading_thinking_block():
    """claude-opus-5 thinks by default: content is [thinking, text]."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from app.services.llm import LLMService

    response = SimpleNamespace(
        usage=SimpleNamespace(input_tokens=1, output_tokens=1,
                              cache_read_input_tokens=0, cache_creation_input_tokens=0),
        content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text='{"ok": true}')])
    svc = LLMService.__new__(LLMService)
    svc._model = "claude-opus-5"
    svc._client = MagicMock()
    svc._client.messages.create = AsyncMock(return_value=response)
    assert asyncio.run(svc.complete([{"role": "user", "content": "x"}])) == '{"ok": true}'


def test_gateway_retries_transient_errors_but_not_permanent_ones(monkeypatch):
    """The gateway path (production agents) had no retries: one SSL 'bad record
    mac' failed a whole diagnosis mid-eval."""
    import asyncio

    import litellm

    from app.services import llm_gateway as gw

    monkeypatch.setattr(gw, "_TRANSIENT_BASE_DELAY", 0.0)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise litellm.InternalServerError("SSL: bad record mac", llm_provider="anthropic", model="m")
        return "ok"

    assert asyncio.run(gw._with_transient_retries(flaky)) == "ok" and calls["n"] == 3

    class Billing(Exception):
        status_code = 400

    async def permanent():
        calls["n"] += 1
        raise Billing("credit balance is too low")

    calls["n"] = 0
    try:
        asyncio.run(gw._with_transient_retries(permanent))
    except Billing:
        pass
    assert calls["n"] == 1

    async def always_down():
        raise litellm.InternalServerError("overloaded", llm_provider="anthropic", model="m")

    try:
        asyncio.run(gw._with_transient_retries(always_down))
        raise AssertionError("should have raised")
    except litellm.InternalServerError:
        pass
