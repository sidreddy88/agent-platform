import asyncio
import logging
import random
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import anthropic

from app.core.config import settings
from app.services.circuit_breaker import circuit_breaker_registry

if TYPE_CHECKING:
    from app.services.tracing import TracingContext

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-20250514"
HAIKU_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 8192

_MAX_RETRIES = 3
_BASE_DELAY_SECONDS = 1.0
_MAX_DELAY_SECONDS = 60.0


def _is_retryable(exc: Exception) -> bool:
    """Transient failures are retryable; deterministic ones are not.

    Classify by status code rather than exception class: Anthropic raises
    529 ("overloaded", the most common transient error in practice) as its
    own OverloadedError subclass, which isn't `InternalServerError` and isn't
    exported from the public `anthropic` namespace. Checking `status_code`
    directly (429, or any 5xx) is what the SDK itself does internally, and
    it doesn't silently miss error subclasses added in future SDK versions.
    """
    if isinstance(exc, (anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return True   # network hiccup / no HTTP response at all
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code == 429 or exc.status_code >= 500
    return False


async def _retry_with_backoff(call_fn):
    """Retry a transient-failure call with exponential backoff + jitter.

    Non-retryable errors (auth, bad request, context overflow, content
    policy, ...) propagate immediately — retrying them can't help and only
    burns rate-limit budget. Runs in front of the circuit breaker: retry
    handles one flaky call, the breaker records the failure only once all
    retries are exhausted.
    """
    for attempt in range(_MAX_RETRIES + 1):
        try:
            return await call_fn()
        except anthropic.APIError as exc:
            if attempt == _MAX_RETRIES or not _is_retryable(exc):
                raise
            delay = min(_BASE_DELAY_SECONDS * (2 ** attempt), _MAX_DELAY_SECONDS)
            jitter = delay * 0.5
            wait = delay + random.uniform(-jitter, jitter)
            logger.warning(
                "[LLMService] retryable error on attempt %d/%d: %s — retrying in %.1fs",
                attempt + 1, _MAX_RETRIES + 1, exc, wait,
            )
            await asyncio.sleep(wait)


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
        system: str | list | None = None,
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
        return await cb.call(_retry_with_backoff(_complete))

    async def stream_chat(
        self,
        messages: list[dict],
        system: str | list | None = None,
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
