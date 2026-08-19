"""
Regression tests for FixGenerationAgent._trace_undefined_source().

Real production bug, found during a README accuracy pass (unrelated to what
was originally being checked): this method called
    self._llm.complete(messages=..., system=..., model="claude-haiku-4-5-20251001")
but LLMService.complete() has no `model` kwarg at all -- every single call
raised TypeError, immediately swallowed by the surrounding `except Exception`,
so this entire mechanism has never actually run once. This is the automated
implementation of "fix the producer, not the crash site" for null/undefined
errors -- retargeting a fix from the consumer (where the crash surfaces) to
the function that actually produces the undefined value. It's a second,
file-generation-time safety net for when DiagnosisAgent's own affected_file/
affected_function doesn't already point at the producer; it has always been
dead.

Fixed by using self._llm_haiku (the same rolling-alias-pinned Haiku instance
_critique_fix already uses) instead of self._llm with an invalid model kwarg
-- this also happens to retire a hardcoded dated model snapshot
("claude-haiku-4-5-20251001") in favor of the centralized rolling alias.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.fix_generation import FixGenerationAgent
from app.models.events import ErrorEvent, EventSource, IncidentState


def _make_agent() -> FixGenerationAgent:
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner = "owner"
    agent._repo = "repo"
    agent._github = MagicMock()
    agent._llm = MagicMock()
    agent._llm.complete = AsyncMock(
        side_effect=TypeError("complete() got an unexpected keyword argument 'model'")
    )
    agent._llm_haiku = MagicMock()
    return agent


def _null_access_incident() -> IncidentState:
    event = ErrorEvent(
        source=EventSource.APPLICATION,
        error_type="TypeError",
        title="Cannot read properties of undefined",
        description="TypeError: Cannot read properties of undefined (reading 'publish_decision')",
        service="svc",
    )
    return IncidentState(error_event=event)


@pytest.mark.asyncio
async def test_uses_llm_haiku_not_llm_with_invalid_model_kwarg():
    """The actual real-world bug: self._llm.complete(..., model=...) always
    raised TypeError. self._llm is deliberately wired to raise exactly that
    in this test -- if the fix regresses back to calling it, this test fails
    with the same TypeError the production code was silently swallowing."""
    agent = _make_agent()
    agent._llm_haiku.complete = AsyncMock(return_value="classifyFields")
    agent._github.search_code = AsyncMock(return_value=[])

    result = await agent._trace_undefined_source(
        "some file content", "consumer.js", _null_access_incident()
    )

    agent._llm_haiku.complete.assert_called_once()
    agent._llm.complete.assert_not_called()
    # No candidates found -> None is the correct (not a crash) outcome here.
    assert result is None
    # Confirm no `model` kwarg was passed to the (correctly-configured) Haiku call.
    _, kwargs = agent._llm_haiku.complete.call_args
    assert "model" not in kwargs


@pytest.mark.asyncio
async def test_resolves_source_function_end_to_end():
    """Full happy path: probe identifies the producer function, code search
    finds a real definition, and the (file, function, content) tuple comes
    back -- proving the whole mechanism now actually works, not just that it
    no longer crashes."""
    agent = _make_agent()
    agent._llm_haiku.complete = AsyncMock(return_value="classifyFields")
    agent._github.search_code = AsyncMock(
        return_value=[{"path": "constants/prankCheckerOpenAI.js"}]
    )
    producer_content = (
        "async function classifyFields(fields) {\n"
        "  return { publish_decision: 'approve' };\n"
        "}\n"
    )
    agent._read_file = AsyncMock(return_value=(producer_content, "sha123"))

    result = await agent._trace_undefined_source(
        "const r = classifyFields(x); r.publish_decision;",
        "constants/prankCheckerMain.js",
        _null_access_incident(),
    )

    assert result == ("constants/prankCheckerOpenAI.js", "classifyFields", producer_content)


@pytest.mark.asyncio
async def test_returns_none_when_not_a_null_access_error():
    """Non-null-access errors should short-circuit before ever calling the LLM."""
    agent = _make_agent()
    agent._llm_haiku.complete = AsyncMock(return_value="classifyFields")
    event = ErrorEvent(
        source=EventSource.APPLICATION,
        error_type="CastError",
        title="Cast to Number failed",
        description="CastError: Cast to Number failed for value 'x'",
        service="svc",
    )
    incident = IncidentState(error_event=event)

    result = await agent._trace_undefined_source("content", "file.js", incident)

    assert result is None
    agent._llm_haiku.complete.assert_not_called()


@pytest.mark.asyncio
async def test_returns_none_when_probe_says_unknown():
    agent = _make_agent()
    agent._llm_haiku.complete = AsyncMock(return_value="UNKNOWN")

    result = await agent._trace_undefined_source(
        "content", "file.js", _null_access_incident()
    )

    assert result is None
