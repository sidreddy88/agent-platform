"""
Tests for TriageAgent's forced-tool-use classification (replaces the old
regex-extract + json.loads + hardcoded-fallback-default parser).

Covers:
  - _submit_triage calls complete_structured with the real schema and
    returns a TriageResult built from its dict output
  - the duplicate-decision structural backstop (schema enum alone can't
    cross-validate decision="duplicate" against what check_duplicate_pr
    actually returned)
  - the still-real fallback path when complete_structured itself fails
  - check_duplicate_pr/get_occurrence_count are called directly, not routed
    through a ReAct loop, when their preconditions are/aren't met
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.triage import _SUBMIT_TRIAGE_SCHEMA, TriageAgent, TriageResult
from app.models.events import ErrorEvent, EventSource


def _make_event(**overrides) -> ErrorEvent:
    defaults = dict(
        source=EventSource.APPLICATION, error_type="TypeError", title="t",
        description="d", service="svc", metadata={},
    )
    defaults.update(overrides)
    return ErrorEvent(**defaults)


def _make_agent() -> TriageAgent:
    agent = TriageAgent(aws=MagicMock(), store=MagicMock())
    agent._store.get_pr_for_resource = MagicMock(return_value=None)
    return agent


class TestSubmitTriageUsesForcedToolUse:
    @pytest.mark.asyncio
    async def test_calls_complete_structured_with_real_schema(self):
        agent = _make_agent()
        agent._llm.complete_structured = AsyncMock(return_value={
            "decision": "real", "severity": "P1", "blast_radius": "single_service",
            "occurrences_24h": 12, "duplicate_pr": None, "reasoning": "x",
        })

        result = await agent.triage(_make_event())

        assert isinstance(result, TriageResult)
        assert result.decision == "real"
        assert result.severity == "P1"
        assert result.occurrences_24h == 12
        schema_used = agent._llm.complete_structured.call_args.kwargs["tool_schema"]
        assert schema_used is _SUBMIT_TRIAGE_SCHEMA
        assert schema_used["input_schema"]["properties"]["decision"]["enum"] == [
            "real", "noise", "duplicate",
        ]

    @pytest.mark.asyncio
    async def test_missing_fields_get_safe_defaults(self):
        """The API guarantees required fields are present in a real response,
        but _submit_triage's .get() defaults are the last line of defense if
        that assumption is ever wrong (e.g. a mocked/malformed test double)."""
        agent = _make_agent()
        agent._llm.complete_structured = AsyncMock(return_value={})

        result = await agent.triage(_make_event())

        assert result.decision == "real"
        assert result.severity == "P2"
        assert result.blast_radius == "unknown"
        assert result.occurrences_24h == 0


class TestDuplicateDecisionBackstop:
    @pytest.mark.asyncio
    async def test_overrides_duplicate_when_no_duplicate_pr_found(self):
        """Schema enum permits decision='duplicate' regardless of context --
        it can't cross-validate against check_duplicate_pr's actual result --
        so this must be enforced in code, same as it was prompt-only before."""
        agent = _make_agent()
        agent._store.get_pr_for_resource = MagicMock(return_value=None)  # NO_DUPLICATE
        agent._llm.complete_structured = AsyncMock(return_value={
            "decision": "duplicate", "severity": "P2", "blast_radius": "unknown",
            "occurrences_24h": 5, "duplicate_pr": "https://github.com/o/r/pull/1",
            "reasoning": "looks like a duplicate",
        })

        result = await agent.triage(_make_event())

        assert result.decision == "real"
        assert result.duplicate_pr is None

    @pytest.mark.asyncio
    async def test_allows_duplicate_when_real_duplicate_pr_found(self):
        agent = _make_agent()
        agent._store.get_pr_for_resource = MagicMock(
            return_value="https://github.com/o/r/pull/42"
        )
        agent._llm.complete_structured = AsyncMock(return_value={
            "decision": "duplicate", "severity": "P2", "blast_radius": "unknown",
            "occurrences_24h": 5, "duplicate_pr": "https://github.com/o/r/pull/42",
            "reasoning": "matches the open PR",
        })

        result = await agent.triage(_make_event())

        assert result.decision == "duplicate"
        assert result.duplicate_pr == "https://github.com/o/r/pull/42"


class TestFallbackOnLlmFailure:
    @pytest.mark.asyncio
    async def test_complete_structured_exception_falls_back_to_real_p2(self):
        agent = _make_agent()
        agent._llm.complete_structured = AsyncMock(side_effect=RuntimeError("provider down"))

        result = await agent.triage(_make_event())

        assert result.decision == "real"
        assert result.severity == "P2"
        assert "provider down" in result.reasoning


class TestDeterministicToolCallsNotRoutedThroughReactLoop:
    @pytest.mark.asyncio
    async def test_check_duplicate_pr_called_with_event_derived_args_directly(self):
        agent = _make_agent()
        agent._store.get_pr_for_resource = MagicMock(return_value=None)
        agent._llm.complete_structured = AsyncMock(return_value={
            "decision": "real", "severity": "P2", "blast_radius": "unknown",
            "occurrences_24h": 0, "duplicate_pr": None, "reasoning": "x",
        })

        event = _make_event(error_type="OOM", service="image-service", description="crash details")
        await agent.triage(event)

        # get_pr_for_resource is called with a key built from event fields --
        # confirms check_duplicate_pr ran directly, not via a model-decided
        # Action Input the ReAct loop would have had to parse first.
        called_keys = [c.args[0] for c in agent._store.get_pr_for_resource.call_args_list]
        assert any("OOM" in k and "image-service" in k for k in called_keys)

    @pytest.mark.asyncio
    async def test_get_occurrence_count_skipped_without_log_group(self):
        agent = _make_agent()
        agent._aws.search_log_events = MagicMock()
        agent._llm.complete_structured = AsyncMock(return_value={
            "decision": "real", "severity": "P2", "blast_radius": "unknown",
            "occurrences_24h": 0, "duplicate_pr": None, "reasoning": "x",
        })

        await agent.triage(_make_event(metadata={}))  # no log_group

        agent._aws.search_log_events.assert_not_called()
        sent_prompt = agent._llm.complete_structured.call_args.kwargs["messages"][0]["content"]
        assert "Not checked" in sent_prompt
