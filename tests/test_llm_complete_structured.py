"""
Tests for LLMService.complete_structured() — forced tool-use completion.

Replaces the regex-extract + json.loads + hardcoded-fallback-default pattern
that used to live in TriageAgent/MergeDecisionAgent with an API-level
structural guarantee: tool_choice locked to a single named tool means the
response can only ever be a call to that tool, matching its declared schema.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.circuit_breaker import circuit_breaker_registry
from app.services.llm import LLMService

_SCHEMA = {
    "name": "submit_thing",
    "description": "Submit a thing.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": ["a", "b"]},
            "note": {"type": "string"},
        },
        "required": ["category", "note"],
    },
}


def _tool_use_response(input_dict: dict, stop_reason: str = "tool_use") -> SimpleNamespace:
    block = SimpleNamespace(type="tool_use", input=input_dict)
    return SimpleNamespace(
        content=[block],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        stop_reason=stop_reason,
    )


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    # complete_structured shares the same "anthropic_llm" circuit breaker as
    # complete() -- reset before/after so a failure in one test can't trip
    # the breaker and fail an unrelated later test.
    circuit_breaker_registry.reset("anthropic_llm")
    yield
    circuit_breaker_registry.reset("anthropic_llm")


class TestCompleteStructured:
    @pytest.mark.asyncio
    async def test_passes_tools_and_forced_tool_choice(self):
        service = LLMService()
        service._client = AsyncMock()
        service._client.messages.create = AsyncMock(
            return_value=_tool_use_response({"category": "a", "note": "n"})
        )

        result = await service.complete_structured(
            messages=[{"role": "user", "content": "hi"}], tool_schema=_SCHEMA,
        )

        assert result == {"category": "a", "note": "n"}
        call_kwargs = service._client.messages.create.call_args.kwargs
        assert call_kwargs["tools"] == [_SCHEMA]
        assert call_kwargs["tool_choice"] == {"type": "tool", "name": "submit_thing"}

    @pytest.mark.asyncio
    async def test_returns_dict_directly_no_parsing_needed(self):
        service = LLMService()
        service._client = AsyncMock()
        service._client.messages.create = AsyncMock(
            return_value=_tool_use_response({"category": "b", "note": "structured, not a string"})
        )

        result = await service.complete_structured(
            messages=[{"role": "user", "content": "hi"}], tool_schema=_SCHEMA,
        )

        assert isinstance(result, dict)
        assert result["note"] == "structured, not a string"

    @pytest.mark.asyncio
    async def test_records_token_usage(self):
        service = LLMService()
        service._client = AsyncMock()
        service._client.messages.create = AsyncMock(
            return_value=_tool_use_response({"category": "a", "note": "n"})
        )

        await service.complete_structured(
            messages=[{"role": "user", "content": "hi"}], tool_schema=_SCHEMA,
        )

        assert service.last_input_tokens == 10
        assert service.last_output_tokens == 5

    @pytest.mark.asyncio
    async def test_raises_clear_error_when_no_tool_use_block(self):
        """Only reachable via a truncated max_tokens response -- tool_choice
        forcing a named tool otherwise guarantees a tool_use block exists."""
        service = LLMService()
        service._client = AsyncMock()
        text_only_response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text="oops, no tool call")],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            stop_reason="max_tokens",
        )
        service._client.messages.create = AsyncMock(return_value=text_only_response)

        with pytest.raises(ValueError, match="No tool_use block"):
            await service.complete_structured(
                messages=[{"role": "user", "content": "hi"}], tool_schema=_SCHEMA,
            )

    @pytest.mark.asyncio
    async def test_passes_system_prompt_through(self):
        service = LLMService()
        service._client = AsyncMock()
        service._client.messages.create = AsyncMock(
            return_value=_tool_use_response({"category": "a", "note": "n"})
        )

        await service.complete_structured(
            messages=[{"role": "user", "content": "hi"}],
            tool_schema=_SCHEMA,
            system="You are a classifier.",
        )

        assert service._client.messages.create.call_args.kwargs["system"] == "You are a classifier."
