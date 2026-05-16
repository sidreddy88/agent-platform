from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import anthropic

from app.core.config import settings
from app.services.circuit_breaker import circuit_breaker_registry

if TYPE_CHECKING:
    from app.services.tracing import TracingContext

MODEL = "claude-sonnet-4-20250514"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 8192


class LLMService:
    def __init__(self, model: str | None = None) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = model or MODEL
        # Updated after every complete() call — read by BaseAgent for checkpointing.
        self.last_input_tokens: int = 0
        self.last_output_tokens: int = 0

    async def complete(
        self,
        messages: list[dict],
        system: str | None = None,
        tracing_ctx: "TracingContext | None" = None,
    ) -> str:
        """Single blocking call — returns full response text. Use for agent loops."""
        kwargs: dict = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        async def _call() -> str:
            response = await self._client.messages.create(**kwargs)
            self.last_input_tokens = (
                response.usage.input_tokens if response.usage else 0
            )
            self.last_output_tokens = (
                response.usage.output_tokens if response.usage else 0
            )
            return response.content[0].text

        async def _complete() -> str:
            if tracing_ctx is not None and tracing_ctx.enabled:
                from app.services.tracing import trace_llm_call
                return await trace_llm_call(tracing_ctx, self._model, messages, system, _call())
            return await _call()

        cb = circuit_breaker_registry.get_or_create(
            "anthropic_llm", failure_threshold=5, timeout_seconds=60.0
        )
        return await cb.call(_complete())

    async def stream_chat(
        self,
        messages: list[dict],
        system: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream text chunks from Claude. Yields one string per token."""
        kwargs: dict = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "messages": messages,
        }
        if system:
            kwargs["system"] = system

        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text
