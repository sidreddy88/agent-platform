"""
Regression tests for the search_log_events() region bug.

Real production bug, traced end-to-end: TargetApp' CloudWatch logs live in
us-east-2; agent-platform's own infra (and its default AWS client) is in
us-east-1. get_error_logs() has always threaded a `region` override through for
exactly this reason -- but search_log_events() never had a region parameter at
all. Every caller (TriageAgent.get_occurrence_count, DiagnosisAgent's
get_error_samples / check_still_occurring / get_occurrence_timeline, and
detection.py's CloudWatch-log-filter pillar) always queried agent-platform's
own default region, which doesn't contain the target log group -- silently
failing every single time, on every single incident, degrading diagnosis
quality (empty evidence, "Unknown" root causes, missing occurrence counts)
without ever surfacing as a loud top-level error, since each tool wraps the
failure in a plain string return instead of raising.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.agents.diagnosis import DiagnosisAgent
from app.agents.triage import TriageAgent
from app.services.aws import AWSService

# ---------------------------------------------------------------------------
# AWSService.search_log_events — the region param itself
# ---------------------------------------------------------------------------

def test_search_log_events_passes_region_to_client():
    service = AWSService.__new__(AWSService)
    mock_logs = MagicMock()
    mock_logs.filter_log_events.return_value = {"events": []}

    with patch.object(AWSService, "_client", return_value=mock_logs) as mock_client:
        service.search_log_events("/ecs/TaskTargetApp", "CastError", region="us-east-2")

    mock_client.assert_called_once_with("logs", region="us-east-2")


def test_search_log_events_region_defaults_to_none():
    """No region passed -> _client's own default region behavior applies,
    same as before this parameter existed."""
    service = AWSService.__new__(AWSService)
    mock_logs = MagicMock()
    mock_logs.filter_log_events.return_value = {"events": []}

    with patch.object(AWSService, "_client", return_value=mock_logs) as mock_client:
        service.search_log_events("/ecs/TaskTargetApp", "CastError")

    mock_client.assert_called_once_with("logs", region=None)


# ---------------------------------------------------------------------------
# TriageAgent.get_occurrence_count — must pass ecs_log_groups_region
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_triage_occurrence_count_uses_configured_log_region():
    agent = TriageAgent.__new__(TriageAgent)
    agent._aws = MagicMock()
    agent._aws.search_log_events = MagicMock(return_value=[])
    agent._store = MagicMock()
    agent._tools = {}

    with patch("app.agents.triage.settings") as mock_settings:
        mock_settings.ecs_log_groups_region = "us-east-2"
        agent._register_tools()
        tool_fn, _ = agent._tools["get_occurrence_count"]
        await tool_fn(log_group="/ecs/TaskTargetApp", pattern="CastError")

    agent._aws.search_log_events.assert_called_once()
    assert agent._aws.search_log_events.call_args.kwargs["region"] == "us-east-2"


# ---------------------------------------------------------------------------
# DiagnosisAgent's three log tools — must all pass ecs_log_groups_region
# ---------------------------------------------------------------------------

def _make_diagnosis_agent() -> DiagnosisAgent:
    agent = DiagnosisAgent.__new__(DiagnosisAgent)
    agent._aws = MagicMock()
    agent._aws.search_log_events = MagicMock(return_value=[])
    agent._rag = None
    agent._github = MagicMock()
    agent._owner = "owner"
    agent._repo = "repo"
    agent._tools = {}
    return agent


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name,kwargs", [
    ("get_error_samples", {"log_group": "/ecs/TaskTargetApp", "pattern": "CastError"}),
    ("check_still_occurring", {"log_group": "/ecs/TaskTargetApp", "pattern": "CastError"}),
    ("get_occurrence_timeline", {"log_group": "/ecs/TaskTargetApp", "pattern": "CastError"}),
])
async def test_diagnosis_log_tool_uses_configured_log_region(tool_name, kwargs):
    agent = _make_diagnosis_agent()

    with patch("app.agents.diagnosis.settings") as mock_settings:
        mock_settings.ecs_log_groups_region = "us-east-2"
        agent._register_tools()
        tool_fn, _ = agent._tools[tool_name]
        await tool_fn(**kwargs)

    agent._aws.search_log_events.assert_called_once()
    assert agent._aws.search_log_events.call_args.kwargs["region"] == "us-east-2"


@pytest.mark.asyncio
async def test_diagnosis_log_tool_region_none_when_unconfigured():
    """Empty ecs_log_groups_region (single-region setups) -> None, not ''."""
    agent = _make_diagnosis_agent()

    with patch("app.agents.diagnosis.settings") as mock_settings:
        mock_settings.ecs_log_groups_region = ""
        agent._register_tools()
        tool_fn, _ = agent._tools["get_error_samples"]
        await tool_fn(log_group="/ecs/svc", pattern="Error")

    assert agent._aws.search_log_events.call_args.kwargs["region"] is None
