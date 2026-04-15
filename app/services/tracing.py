"""
Tracing service — wraps Langfuse v4 to capture every agent invocation,
LLM call, and tool execution as structured traces.

What gets traced:
  Agent run   → top-level span (as_type="agent")
  LLM call    → generation span nested under the agent span
  Tool call   → tool span nested under the agent span

When LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set in .env,
tracing is silently disabled — agents run exactly as before.

Langfuse v4 uses context managers + contextvars to automatically nest
spans. No TracingContext object needs to be passed around — the SDK
tracks the current observation internally per asyncio task.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Langfuse client — lazy singleton, None when keys are not configured
# ---------------------------------------------------------------------------

_langfuse: Any = None
_initialized = False


def _get_client() -> Any:
    global _langfuse, _initialized
    if _initialized:
        return _langfuse

    _initialized = True

    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.debug("[Tracing] Langfuse keys not set — tracing disabled")
        return None

    try:
        from langfuse import Langfuse
        host = settings.langfuse_base_url or settings.langfuse_host
        _langfuse = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            host=host,
        )
        # Set service name for OpenTelemetry resource attributes
        import os
        os.environ.setdefault("OTEL_SERVICE_NAME", settings.app_name)
        logger.info("[Tracing] Langfuse v4 initialized → %s", host)
    except Exception as exc:
        logger.warning("[Tracing] Failed to initialize Langfuse: %s", exc)
        _langfuse = None

    return _langfuse


# ---------------------------------------------------------------------------
# Backward-compat no-op TracingContext
# (base.py and llm.py still reference self._tracing_ctx)
# ---------------------------------------------------------------------------

class TracingContext:
    """No-op stub kept for backward compatibility. Tracing is now handled
    via Langfuse v4 context managers directly in the wrapper functions."""

    def __init__(self, trace: Any = None, enabled: bool = False) -> None:
        self.enabled = enabled

    def span(self, name: str, input: Any = None) -> Any:
        return _NoOpSpan()

    def generation(self, name: str, model: str, input: Any = None) -> Any:
        return _NoOpSpan()

    def flush(self) -> None:
        pass


class _NoOpSpan:
    def end(self, **kwargs: Any) -> None:
        pass

    def update(self, **kwargs: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# trace_agent — decorator for BaseAgent.run()
# ---------------------------------------------------------------------------

def trace_agent(run_method: Callable) -> Callable:
    """
    Wraps BaseAgent.run() with a Langfuse v4 agent span.

    All nested LLM calls and tool calls automatically become child spans
    because Langfuse v4 tracks the current observation via contextvars.
    """

    @functools.wraps(run_method)
    async def wrapper(self: Any, user_input: str) -> Any:
        # Keep no-op TracingContext for backward compat
        self._tracing_ctx = TracingContext()

        lf = _get_client()
        agent_name = type(self).__name__
        start = time.perf_counter()

        if lf is None:
            result = await run_method(self, user_input)
            _record_latency(agent_name, start)
            return result

        try:
            with lf.start_as_current_observation(
                name=agent_name,
                as_type="agent",
                input={"user_input": user_input},
            ) as obs:
                obs.update(tags=[settings.environment])
                try:
                    result = await run_method(self, user_input)
                    duration_ms = int((time.perf_counter() - start) * 1000)
                    obs.update(
                        output={"answer": result.answer, "iterations": result.iterations},
                        metadata={"agent": agent_name, "iterations": result.iterations,
                                  "duration_ms": duration_ms},
                    )
                    return result
                except Exception as exc:
                    duration_ms = int((time.perf_counter() - start) * 1000)
                    obs.update(
                        level="ERROR",
                        status_message=str(exc),
                        metadata={"agent": agent_name, "duration_ms": duration_ms},
                    )
                    raise
        finally:
            _record_latency(agent_name, start)
            try:
                lf.flush()
            except Exception:
                pass

    return wrapper


# ---------------------------------------------------------------------------
# trace_llm_call — wraps a single LLMService.complete() call
# ---------------------------------------------------------------------------

async def trace_llm_call(
    ctx: TracingContext,
    model: str,
    messages: list[dict],
    system: str | None,
    coro: Coroutine,
) -> str:
    lf = _get_client()
    if lf is None:
        return await coro

    start = time.perf_counter()
    try:
        with lf.start_as_current_observation(
            name="llm_call",
            as_type="generation",
            model=model,
            input={"system": system or "", "messages": messages},
        ) as gen:
            try:
                response = await coro
                duration_ms = int((time.perf_counter() - start) * 1000)
                gen.update(output=response, metadata={"duration_ms": duration_ms})
                return response
            except Exception as exc:
                gen.update(level="ERROR", status_message=str(exc))
                raise
    except Exception:
        raise


# ---------------------------------------------------------------------------
# trace_tool_call — wraps a single tool execution
# ---------------------------------------------------------------------------

async def trace_tool_call(
    ctx: TracingContext,
    tool_name: str,
    tool_input: Any,
    coro: Coroutine,
) -> str:
    lf = _get_client()
    if lf is None:
        return await coro

    start = time.perf_counter()
    try:
        with lf.start_as_current_observation(
            name=tool_name,
            as_type="tool",
            input=tool_input,
        ) as span:
            try:
                result = await coro
                duration_ms = int((time.perf_counter() - start) * 1000)
                span.update(
                    output=result[:500] if isinstance(result, str) else result,
                    metadata={"duration_ms": duration_ms},
                )
                return result
            except Exception as exc:
                span.update(level="ERROR", status_message=str(exc))
                raise
    except Exception:
        raise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _record_latency(agent_name: str, start: float) -> None:
    try:
        from app.services.latency import latency_tracker
        latency_tracker.record(agent_name, int((time.perf_counter() - start) * 1000))
    except Exception:
        pass
