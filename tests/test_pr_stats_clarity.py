"""
Regression tests for GET /agents/pr-stats including ErrorClarityAgent PRs.

Real gap: this endpoint (backs the "Agent PRs" dashboard's mark-merged/stats UI)
only ever filtered on incident.pr_number/incident.pr_url — FixGenerationAgent's
fields. An ErrorClarityAgent PR (clarity_pr_number/clarity_pr_url) was invisible
here entirely, even after a human merged it on GitHub: no row in the dashboard,
no way to mark it merged, no stats. incidents.py's /mark-merged route itself is
already generic (just flips status/outcome, doesn't care which PR field is set) —
the only actual bug was this endpoint's list filter and per-incident PR lookups
never considering the clarity fields at all.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.routes.agents import get_pr_stats
from app.models.events import ErrorEvent, EventSource, IncidentState, IncidentStatus, Severity


def _make_incident(**overrides) -> IncidentState:
    event = ErrorEvent(
        source=EventSource.CLOUDWATCH,
        title="Some error",
        description="Some error description",
        service="TaskAllInterviews",
        severity=Severity.P3,
    )
    defaults = dict(
        id="incident-1",
        error_event=event,
        status=IncidentStatus.RESOLVED,
        outcome=None,
        pr_url=None,
        pr_number=None,
        clarity_pr_url=None,
        clarity_pr_number=None,
    )
    defaults.update(overrides)
    return IncidentState(**defaults)


@pytest.fixture(autouse=True)
def _no_github_calls():
    """Avoid any real network calls — GitHubService is imported inside the route
    function itself, so patching the class where it's defined is enough."""
    mock_gh_cls = MagicMock()
    mock_gh = MagicMock()
    mock_gh.get_workflow_runs = AsyncMock(return_value=[])
    mock_gh.get_pr = AsyncMock(side_effect=Exception("should not be called without owner/repo"))
    mock_gh.get_commit_checks = AsyncMock(return_value=[])
    mock_gh_cls.return_value = mock_gh
    with patch("app.services.github.GitHubService", mock_gh_cls):
        yield mock_gh


@pytest.fixture(autouse=True)
def _no_agent_tracker(monkeypatch):
    monkeypatch.setattr(
        "app.api.routes.agents.agent_tracker.get_runs_for_incident",
        lambda incident_id: [],
    )


@pytest.mark.asyncio
async def test_clarity_only_incident_is_included():
    clarity_incident = _make_incident(
        id="clarity-1",
        clarity_pr_url="https://github.com/o/r/pull/99",
        clarity_pr_number=99,
        outcome="fix_merged",
    )
    monkeypatched = MagicMock()
    monkeypatched.list_all = MagicMock(return_value=[clarity_incident])
    with patch("app.api.routes.agents.incident_store", monkeypatched):
        result = await get_pr_stats()

    ids = [r["incident_id"] for r in result["prs"]]
    assert "clarity-1" in ids


@pytest.mark.asyncio
async def test_clarity_incident_reports_clarity_pr_fields():
    clarity_incident = _make_incident(
        id="clarity-1",
        clarity_pr_url="https://github.com/o/r/pull/99",
        clarity_pr_number=99,
        outcome="fix_merged",
    )
    monkeypatched = MagicMock()
    monkeypatched.list_all = MagicMock(return_value=[clarity_incident])
    with patch("app.api.routes.agents.incident_store", monkeypatched):
        result = await get_pr_stats()

    row = next(r for r in result["prs"] if r["incident_id"] == "clarity-1")
    assert row["pr_url"] == "https://github.com/o/r/pull/99"
    assert row["pr_number"] == 99
    assert row["is_clarity_pr"] is True


@pytest.mark.asyncio
async def test_fix_incident_is_not_flagged_as_clarity():
    fix_incident = _make_incident(
        id="fix-1", pr_url="https://github.com/o/r/pull/50", pr_number=50,
        outcome="fix_merged",
    )
    monkeypatched = MagicMock()
    monkeypatched.list_all = MagicMock(return_value=[fix_incident])
    with patch("app.api.routes.agents.incident_store", monkeypatched):
        result = await get_pr_stats()

    row = next(r for r in result["prs"] if r["incident_id"] == "fix-1")
    assert row["pr_url"] == "https://github.com/o/r/pull/50"
    assert row["pr_number"] == 50
    assert row["is_clarity_pr"] is False


@pytest.mark.asyncio
async def test_incident_with_no_pr_at_all_is_excluded():
    no_pr_incident = _make_incident(id="no-pr-1")
    monkeypatched = MagicMock()
    monkeypatched.list_all = MagicMock(return_value=[no_pr_incident])
    with patch("app.api.routes.agents.incident_store", monkeypatched):
        result = await get_pr_stats()

    ids = [r["incident_id"] for r in result["prs"]]
    assert "no-pr-1" not in ids


@pytest.mark.asyncio
async def test_summary_counts_include_clarity_merges():
    clarity_incident = _make_incident(
        id="clarity-1",
        clarity_pr_url="https://github.com/o/r/pull/99",
        clarity_pr_number=99,
        outcome="fix_merged",
    )
    monkeypatched = MagicMock()
    monkeypatched.list_all = MagicMock(return_value=[clarity_incident])
    with patch("app.api.routes.agents.incident_store", monkeypatched):
        result = await get_pr_stats()

    assert result["summary"]["total"] == 1
    assert result["summary"]["merged"] == 1
