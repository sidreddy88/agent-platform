import asyncio
import logging
import random
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import anthropic

from app.core.config import settings
from app.services.circuit_breaker import circuit_breaker_registry
from app.services.model_config import DEFAULT_MODELS

if TYPE_CHECKING:
    from app.services.tracing import TracingContext

logger = logging.getLogger(__name__)

# Sourced from config/llm_routing.json's "defaults" section via
# app.services.model_config — see that module's docstring for why these
# aren't hardcoded here directly.
MODEL = DEFAULT_MODELS["sonnet"]
HAIKU_MODEL = DEFAULT_MODELS["haiku"]
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
                from app.services.alerting import alerting_service
                await alerting_service.check_provider_error(exc)
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
    def __init__(self, model: str | None = None, temperature: float | None = None) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self._model = model or MODEL
        # None (default) omits temperature entirely, preserving the Anthropic
        # API's own default (1.0) -- unchanged behavior for every existing
        # caller. Callers that need reproducible output (e.g. TriageAgent,
        # for regression-eval stability) pass an explicit value.
        self._temperature = temperature
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
        # getattr, not self._temperature directly: several existing tests
        # construct LLMService via __new__ (bypassing __init__) and only set
        # the attributes they care about -- _temperature didn't exist before
        # this change, so a direct attribute access breaks them with an
        # AttributeError instead of falling back to "unset".
        # extra_body, not a direct kwarg: anthropic>=1.0 (the SDK's newest
        # major version, which requirements.txt's unbounded ">=0.40.0" pin
        # allows -- caught this in CI, not locally, since local stayed on an
        # older cached 0.x install) removed `temperature` from
        # messages.create()'s typed signature entirely. extra_body is the
        # SDK's own documented escape hatch for exactly this -- passes
        # through to the raw request body regardless of SDK version, so this
        # works on both the old and new major version without pinning either.
        if getattr(self, "_temperature", None) is not None:
            kwargs["extra_body"] = {"temperature": self._temperature}

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
