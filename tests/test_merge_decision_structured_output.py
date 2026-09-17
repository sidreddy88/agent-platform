"""
Tests for MergeDecisionAgent's forced-tool-use decision (replaces the old
regex-extract + json.loads + hardcoded-fallback-default parser).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.merge_decision import (
    _SUBMIT_MERGE_DECISION_SCHEMA,
    MergeDecision,
    MergeDecisionAgent,
)
from app.models.events import ErrorEvent, EventSource, IncidentState


def _make_incident() -> IncidentState:
    event = ErrorEvent(source=EventSource.APPLICATION, error_type="E", title="t",
                        description="d", service="svc")
    return IncidentState(error_event=event)


class TestDecideUsesForcedToolUse:
    @pytest.mark.asyncio
    async def test_calls_complete_structured_with_real_schema(self):
        agent = MergeDecisionAgent(llm=MagicMock())
        agent._llm.complete_structured = AsyncMock(return_value={
            "decision": "merge_now", "reasoning": "core fix is correct",
            "blocking_issues": [], "non_blocking_issues": ["missing tests"],
        })

        result = await agent.decide(_make_incident(), "LGTM aside from missing tests")

        assert isinstance(result, MergeDecision)
        assert result.decision == "merge_now"
        assert result.non_blocking_issues == ["missing tests"]
        schema_used = agent._llm.complete_structured.call_args.kwargs["tool_schema"]
        assert schema_used is _SUBMIT_MERGE_DECISION_SCHEMA
        assert schema_used["input_schema"]["properties"]["decision"]["enum"] == [
            "merge_now", "refix_first",
        ]

    @pytest.mark.asyncio
    async def test_missing_fields_get_safe_defaults(self):
        agent = MergeDecisionAgent(llm=MagicMock())
        agent._llm.complete_structured = AsyncMock(return_value={})

        result = await agent.decide(_make_incident(), "some review")

        assert result.decision == "refix_first"
        assert result.blocking_issues == []
        assert result.non_blocking_issues == []


class TestFallbackOnLlmFailure:
    @pytest.mark.asyncio
    async def test_complete_structured_exception_falls_back_to_refix_first(self):
        agent = MergeDecisionAgent(llm=MagicMock())
        agent._llm.complete_structured = AsyncMock(side_effect=RuntimeError("provider down"))

        result = await agent.decide(_make_incident(), "some review")

        assert result.decision == "refix_first"
        assert "provider down" in result.reasoning
        assert result.blocking_issues == []
        assert result.non_blocking_issues == []
