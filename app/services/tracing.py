"""
Tracing service — wraps Langfuse to capture every agent invocation,
LLM call, and tool execution as structured traces.

What gets traced:
  Agent run   → top-level Langfuse trace (input, output, duration, metadata)
  LLM call    → generation span (prompt, response, model, token counts, latency)
  Tool call   → event span (tool name, input, output, duration, errors)

When LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are not set in .env,
tracing is silently disabled — agents run exactly as before.

Usage:
  Tracing is wired into BaseAgent and LLMService automatically.
  No changes needed in individual agents.

  To view traces: https://cloud.langfuse.com  (or your self-hosted instance)
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable, Coroutine
from contextlib import contextmanager
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Langfuse client — lazy singleton, None when keys are not configured
# ---------------------------------------------------------------------------

_langfuse: Any = None
_initialized = False


def _get_client() -> Any:
    """Return the Langfuse client, initializing it once. Returns None if unconfigured."""
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
        logger.info("[Tracing] Langfuse initialized → %s", settings.langfuse_host)
    except Exception as exc:
        logger.warning("[Tracing] Failed to initialize Langfuse: %s", exc)
        _langfuse = None

    return _langfuse


# ---------------------------------------------------------------------------
# TracingContext — holds the active trace for the duration of one agent run
# ---------------------------------------------------------------------------

class TracingContext:
    """
    Lightweight context object passed through a single agent run.
    Holds the active Langfuse trace so all spans nest under it correctly.
    """

    def __init__(self, trace: Any, enabled: bool) -> None:
        self._trace = trace
        self.enabled = enabled

    def span(self, name: str, input: Any = None) -> Any:
        """Open a new child span. Returns the span, or a no-op sentinel."""
        if not self.enabled or self._trace is None:
            return _NoOpSpan()
        try:
            return self._trace.span(name=name, input=input)
        except Exception:
            return _NoOpSpan()

    def generation(self, name: str, model: str, input: Any = None) -> Any:
        """Open a generation span for an LLM call."""
        if not self.enabled or self._trace is None:
            return _NoOpSpan()
        try:
            return self._trace.generation(name=name, model=model, input=input)
        except Exception:
            return _NoOpSpan()

    def flush(self) -> None:
        if self.enabled and self._trace is not None:
            try:
                lf = _get_client()
                if lf:
                    lf.flush()
            except Exception:
                pass


class _NoOpSpan:
    """Returned when tracing is disabled — all method calls are silent no-ops."""
    def end(self, **kwargs: Any) -> None:
        pass

    def update(self, **kwargs: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# trace_agent decorator
# ---------------------------------------------------------------------------

def trace_agent(run_method: Callable) -> Callable:
    """
    Decorator for BaseAgent.run().

    Creates a top-level Langfuse trace for each agent invocation and
    attaches it to the agent as `self._tracing_ctx` so LLM and tool
    spans can nest under it.

    Usage: applied automatically in BaseAgent — no manual decoration needed.
    """
    @functools.wraps(run_method)
    async def wrapper(self: Any, user_input: str) -> Any:
        lf = _get_client()
        agent_name = type(self).__name__
        enabled = lf is not None

        trace = None
        if enabled:
            try:
                trace = lf.trace(
                    name=agent_name,
                    input={"user_input": user_input},
                    metadata={"agent": agent_name},
                )
            except Exception as exc:
                logger.debug("[Tracing] Could not create trace: %s", exc)
                enabled = False

        ctx = TracingContext(trace=trace, enabled=enabled)
        self._tracing_ctx = ctx
        start = time.perf_counter()

        try:
            result = await run_method(self, user_input)
            duration_ms = int((time.perf_counter() - start) * 1000)

            if enabled and trace:
                try:
                    trace.update(
                        output={"answer": result.answer, "iterations": result.iterations},
                        metadata={
                            "agent": agent_name,
                            "iterations": result.iterations,
                            "duration_ms": duration_ms,
                        },
                    )
                except Exception:
                    pass

            return result

        except Exception as exc:
            duration_ms = int((time.perf_counter() - start) * 1000)
            if enabled and trace:
                try:
                    trace.update(
                        output={"error": str(exc)},
                        level="ERROR",
                        status_message=str(exc),
                        metadata={"duration_ms": duration_ms},
                    )
                except Exception:
                    pass
            raise

        finally:
            ctx.flush()

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
    """
    Wrap a single LLM completion call with a Langfuse generation span.

    Called from LLMService.complete() when a TracingContext is active.
    """
    if not ctx.enabled:
        return await coro

    prompt_input = {
        "messages": messages,
        "system": system or "",
    }

    gen = ctx.generation(name="llm_call", model=model, input=prompt_input)
    start = time.perf_counter()

    try:
        response = await coro
        duration_ms = int((time.perf_counter() - start) * 1000)

        try:
            gen.end(
                output=response,
                metadata={"duration_ms": duration_ms},
            )
        except Exception:
            pass

        return response

    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        try:
            gen.end(
                output={"error": str(exc)},
                level="ERROR",
                status_message=str(exc),
                metadata={"duration_ms": duration_ms},
            )
        except Exception:
            pass
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
    """
    Wrap a tool execution with a Langfuse span.

    Called from BaseAgent._execute_tool() automatically.
    """
    if not ctx.enabled:
        return await coro

    span = ctx.span(name=f"tool:{tool_name}", input=tool_input)
    start = time.perf_counter()

    try:
        result = await coro
        duration_ms = int((time.perf_counter() - start) * 1000)

        try:
            span.end(
                output=result[:500] if isinstance(result, str) else result,
                metadata={"duration_ms": duration_ms},
            )
        except Exception:
            pass

        return result

    except Exception as exc:
        duration_ms = int((time.perf_counter() - start) * 1000)
        try:
            span.end(
                output={"error": str(exc)},
                level="ERROR",
                status_message=str(exc),
                metadata={"duration_ms": duration_ms},
            )
        except Exception:
            pass
        raise
