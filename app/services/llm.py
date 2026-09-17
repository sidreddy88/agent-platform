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

# Per-attempt HTTP timeout. Non-streaming, so the whole generation (up to
# MAX_TOKENS of output, ~2-3 min on Sonnet) arrives before the response
# headers -- the read timeout has to cover all of it. 5 min leaves room for a
# slow response while bounding a stalled one.
#
# Why this exists: in gate run 36158424400, matplotlib__matplotlib-22865 sat
# for 30 min waiting on response headers (anthropic -> httpx -> TLS read).
# The client used the SDK default (600s per attempt, 2 SDK retries) *inside*
# _retry_with_backoff's own 3 retries, which also retry timeouts: up to 12
# attempts x 10 min, about 2 hours, for one stalled call. That applies to
# production incidents as well as the gate. The SDK's own retries are turned
# off so _retry_with_backoff is the only retry layer; worst case is now
# 4 attempts x 5 min.
_REQUEST_TIMEOUT = anthropic.Timeout(300.0, connect=10.0)
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


def _mark_system_for_cache(system: str | list) -> list:
    """Return system as content blocks with a cache breakpoint on the last one.

    Leaves any breakpoint the caller already placed (BaseAgent._with_harness
    marks its harness-docs block) and never mutates the caller's list.
    """
    if isinstance(system, str):
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    blocks = [dict(b) for b in system]
    if blocks and "cache_control" not in blocks[-1]:
        blocks[-1]["cache_control"] = {"type": "ephemeral"}
    return blocks


class LLMService:
    def __init__(self, model: str | None = None, temperature: float | None = None) -> None:
        self._client = anthropic.AsyncAnthropic(
            api_key=settings.anthropic_api_key,
            timeout=_REQUEST_TIMEOUT,
            max_retries=0,   # _retry_with_backoff is the one retry layer
        )
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
        cache: bool = False,
    ) -> str:
        """Single blocking call — returns full response text. Use for agent loops.

        cache=True turns on prompt caching for a multi-turn loop: an explicit
        breakpoint on the last system block (the static prefix -- tool
        descriptions and instructions) plus top-level automatic caching, which
        moves a breakpoint to the end of the growing conversation each turn.
        Off by default: a one-shot call pays the 1.25x cache-write premium on
        its whole prompt and never reads it back.

        Why: the #255 gate run (36158424400) measured $91.63 for 56 cases with
        a 0.0% cache hit rate -- $61.06 of it uncached input -- because the
        only cache_control in the codebase sat on harness docs, which are
        absent outside the target app, and nothing marked the conversation a
        ReAct loop resends on every one of up to 15 turns.
        """
        kwargs: dict = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "messages": messages,
        }
        if system:
            kwargs["system"] = _mark_system_for_cache(system) if cache else system
        if cache:
            kwargs["cache_control"] = {"type": "ephemeral"}
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
            if response.usage:
                # input_tokens is uncached input only; cache traffic is
                # reported separately and was previously dropped here.
                from app.services import cost_meter
                cost_meter.record(
                    self._model,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                    getattr(response.usage, "cache_read_input_tokens", 0) or 0,
                    getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
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

    async def complete_structured(
        self,
        messages: list[dict],
        tool_schema: dict,
        system: str | list | None = None,
        tracing_ctx: "TracingContext | None" = None,
    ) -> dict:
        """Single call, forced into a specific tool call via tool_choice — returns
        the tool call's input dict directly. No regex, no json.loads, no fence-
        stripping, no silent fallback-on-parse-failure: the API itself makes any
        other response shape impossible (every `required` field present, every
        `enum` field one of its declared values), rather than asking for JSON in
        prose and hoping the model formats it the way the prompt asked. See
        TriageAgent.triage()/MergeDecisionAgent.decide() for the two real callers
        this replaced a regex-extract-then-fallback-default parser in.

        Doesn't guarantee the *values* are stable run to run -- the model is
        still doing real, non-deterministic reasoning about content. It
        guarantees the *shape* always parses, which is the part application
        code actually needs a contract for.
        """
        kwargs: dict = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "messages": messages,
            "tools": [tool_schema],
            "tool_choice": {"type": "tool", "name": tool_schema["name"]},
        }
        if system:
            kwargs["system"] = system
        if getattr(self, "_temperature", None) is not None:
            kwargs["extra_body"] = {"temperature": self._temperature}

        async def _call() -> dict:
            response = await self._client.messages.create(**kwargs)
            self.last_input_tokens = (
                response.usage.input_tokens if response.usage else 0
            )
            self.last_output_tokens = (
                response.usage.output_tokens if response.usage else 0
            )
            tool_use = next((b for b in response.content if b.type == "tool_use"), None)
            if tool_use is None:
                # Only reachable via a truncated response (stop_reason ==
                # "max_tokens" cutting off the tool call mid-generation) --
                # tool_choice forcing a named tool otherwise guarantees this
                # block exists. Surface clearly rather than let a caller's
                # dict-shaped assumptions fail confusingly downstream.
                raise ValueError(
                    f"No tool_use block in forced tool_choice response "
                    f"(stop_reason={response.stop_reason!r}) — likely truncated at max_tokens."
                )
            return tool_use.input

        async def _complete() -> dict:
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
