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
