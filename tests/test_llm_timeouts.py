"""LLMService's Anthropic client: bounded per-attempt timeout, one retry layer."""
def test_llm_client_has_bounded_timeout_and_a_single_retry_layer():
    from app.services.llm import LLMService

    client = LLMService()._client
    assert client.max_retries == 0
    assert client.timeout.read == 300.0 and client.timeout.connect == 10.0
