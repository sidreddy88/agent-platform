"""
Tests for DiagnosisAgent's deterministic log-context fetch — steps 1-3
(get_error_samples, check_still_occurring, get_occurrence_timeline) are now
called directly in Python instead of routed through the ReAct loop, since
their arguments (log_group/pattern from event.metadata, minutes/hours fixed
literals) never required model judgment. Same anti-pattern found and fixed
in TriageAgent; confirmed here via scripts/audit_deterministic_tool_calls.py.

First test of DiagnosisAgent.diagnose() end-to-end at all in this test suite
(existing tests only exercise its sub-components in isolation) -- kept
minimal and focused on this one behavior, not a general diagnose() test.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.base import AgentResult
from app.agents.diagnosis import DiagnosisAgent, DiagnosisResult
from app.models.events import ErrorEvent, EventSource, IncidentState


def _make_agent() -> DiagnosisAgent:
    agent = DiagnosisAgent.__new__(DiagnosisAgent)
    agent._owner, agent._repo = "owner", "repo"
    agent._aws = MagicMock()
    agent._rag = None
    agent._github = MagicMock()
    agent._local_repo = MagicMock(ready=False, pinned=False)
    agent._local_repo.ensure_fresh = AsyncMock()
    agent._local_repo.list_files = MagicMock(return_value=[])
    agent._tools = {}
    agent._diagnosis_submitted = None
    agent._rejection_count = 0
    agent._last_retrieved_chunks = []
    agent._retrieved_file_paths = set()
    agent._register_tools()
    return agent


def _make_incident(**event_overrides) -> IncidentState:
    defaults = dict(
        source=EventSource.APPLICATION, error_type="NoSuchKey", title="S3 NoSuchKey",
        description="S3 threw NoSuchKey while deleting a file", service="image-service",
        metadata={"log_group": "/ecs/image-service", "pattern": "NoSuchKey"},
    )
    defaults.update(event_overrides)
    event = ErrorEvent(**defaults)
    return IncidentState(error_event=event)


def _stub_successful_run(agent: DiagnosisAgent, prompts: list):
    """Mock self.run() to skip the real ReAct loop entirely, capture the
    prompt it was given, and simulate a submission having already passed
    grounding -- the real submit_diagnosis tool handler is what normally
    sets _diagnosis_submitted; bypassed here since this test is about the
    log-fetch behavior, not the grounding gate.
    """
    async def fake_run(prompt):
        prompts.append(prompt)
        agent._diagnosis_submitted = DiagnosisResult(root_cause="stub", confidence=0.9)
        return AgentResult(answer="stub", steps=[], iterations=1)
    agent.run = AsyncMock(side_effect=fake_run)


class TestDeterministicLogFetch:
    @pytest.mark.asyncio
    async def test_log_tools_called_directly_with_correct_args_when_log_group_present(self):
        agent = _make_agent()
        agent._aws.search_log_events = MagicMock(return_value=[
            {"timestamp": "2026-09-18T10:00:00", "stream": "ecs/task/abc12345", "message": "NoSuchKey error"},
        ])
        prompts: list[str] = []
        _stub_successful_run(agent, prompts)

        await agent.diagnose(_make_incident())

        # search_log_events backs all three tools -- called 3 times total,
        # confirming get_error_samples/check_still_occurring/
        # get_occurrence_timeline all actually ran.
        assert agent._aws.search_log_events.call_count == 3
        calls = agent._aws.search_log_events.call_args_list
        assert calls[0].kwargs["minutes"] == 120          # get_error_samples
        assert calls[1].kwargs["minutes"] == 10           # check_still_occurring (fixed internally)
        assert calls[2].kwargs["minutes"] == 24 * 60      # get_occurrence_timeline hours=24

        sent_prompt = prompts[0]
        assert "STEPS 1-3 — LOG CONTEXT (already fetched, no tool call needed)" in sent_prompt
        assert "NoSuchKey error" in sent_prompt  # real get_error_samples output embedded

    @pytest.mark.asyncio
    async def test_log_tools_never_called_when_log_group_missing(self):
        agent = _make_agent()
        agent._aws.search_log_events = MagicMock()
        prompts: list[str] = []
        _stub_successful_run(agent, prompts)

        await agent.diagnose(_make_incident(metadata={}))  # no log_group

        agent._aws.search_log_events.assert_not_called()
        sent_prompt = prompts[0]
        assert "log_group is not set for this incident" in sent_prompt
        assert "cap confidence at 0.75" in sent_prompt

    @pytest.mark.asyncio
    async def test_error_samples_still_available_for_step_5_search_terms(self):
        """The prompt still tells the model to mine step 1's real output for
        search terms -- confirms the pre-fetched content is actually usable,
        not just present as a label."""
        agent = _make_agent()
        agent._aws.search_log_events = MagicMock(return_value=[
            {"timestamp": "2026-09-18T10:00:00", "stream": "ecs/task/abc12345",
             "message": "TypeError in classifyFields at routes/services/classify.js:42"},
        ])
        prompts: list[str] = []
        _stub_successful_run(agent, prompts)

        await agent.diagnose(_make_incident())

        sent_prompt = prompts[0]
        assert "classifyFields" in sent_prompt
        assert "search terms for step 5" in sent_prompt
