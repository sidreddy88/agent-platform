"""
Tests for the PR merge auto-resolution poller.

Covers:
  - AWAITING_APPROVAL incident with a merged PR → auto-resolved
  - AWAITING_APPROVAL incident with an open PR → stays AWAITING_APPROVAL
  - Incidents in other statuses are ignored
  - Approval record is marked approved on auto-resolution
  - GitHub API error → incident unchanged

Run:
    pytest tests/test_pr_merge_poller.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.events import ErrorEvent, EventSource, IncidentState, IncidentStatus


def make_incident(status: IncidentStatus, pr_number: int | None = 42) -> IncidentState:
    event = ErrorEvent(
        source=EventSource.APPLICATION,
        error_type="NULL_PTR",
        title="NPE in processOrder",
        description="NullPointerException",
        service="order-service",
    )
    incident = IncidentState(error_event=event)
    incident.confidence = 0.9
    incident.pr_number = pr_number
    incident.pr_url = f"https://github.com/org/repo/pull/{pr_number}" if pr_number else None
    incident.approval_id = "approval-001"
    incident.status = status
    return incident


class TestCheckMergedPRs:
    @pytest.mark.asyncio
    async def test_merged_pr_resolves_incident(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        incident = make_incident(IncidentStatus.AWAITING_APPROVAL)

        mock_store = MagicMock()
        mock_store.list_all = MagicMock(return_value=[incident])
        mock_store.update = MagicMock()

        mock_gh = AsyncMock()
        mock_gh.is_pr_merged = AsyncMock(return_value=True)

        mock_approvals = MagicMock()
        mock_approvals.approve = MagicMock()

        with patch("app.services.incident_loop.incident_store", mock_store), \
             patch("app.services.incident_loop.approval_service", mock_approvals), \
             patch("app.services.incident_loop.settings") as mock_settings, \
             patch("app.services.github.GitHubService", return_value=mock_gh):
            mock_settings.fix_target_repo = "org/repo"
            await loop._check_merged_prs()

        assert incident.status == IncidentStatus.RESOLVED
        assert incident.human_decision == "approved"
        assert incident.outcome == "fix_merged"
        assert incident.resolved_at is not None
        mock_store.update.assert_called_once_with(incident)
        mock_approvals.approve.assert_called_once_with("approval-001", "github_merged")

    @pytest.mark.asyncio
    async def test_open_pr_leaves_incident_unchanged(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        incident = make_incident(IncidentStatus.AWAITING_APPROVAL)

        mock_store = MagicMock()
        mock_store.list_all = MagicMock(return_value=[incident])
        mock_store.update = MagicMock()

        mock_gh = AsyncMock()
        mock_gh.is_pr_merged = AsyncMock(return_value=False)

        with patch("app.services.incident_loop.incident_store", mock_store), \
             patch("app.services.incident_loop.settings") as mock_settings, \
             patch("app.services.github.GitHubService", return_value=mock_gh):
            mock_settings.fix_target_repo = "org/repo"
            await loop._check_merged_prs()

        assert incident.status == IncidentStatus.AWAITING_APPROVAL
        mock_store.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_awaiting_approval_incidents_are_skipped(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        reviewing = make_incident(IncidentStatus.REVIEWING)
        resolved = make_incident(IncidentStatus.RESOLVED)
        fixing = make_incident(IncidentStatus.FIXING)

        mock_store = MagicMock()
        mock_store.list_all = MagicMock(return_value=[reviewing, resolved, fixing])

        mock_gh = AsyncMock()

        with patch("app.services.incident_loop.incident_store", mock_store), \
             patch("app.services.incident_loop.settings") as mock_settings, \
             patch("app.services.github.GitHubService", return_value=mock_gh):
            mock_settings.fix_target_repo = "org/repo"
            await loop._check_merged_prs()

        mock_gh.is_pr_merged.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_incident_without_pr_number_is_skipped(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        incident = make_incident(IncidentStatus.AWAITING_APPROVAL, pr_number=None)

        mock_store = MagicMock()
        mock_store.list_all = MagicMock(return_value=[incident])

        mock_gh = AsyncMock()

        with patch("app.services.incident_loop.incident_store", mock_store), \
             patch("app.services.incident_loop.settings") as mock_settings, \
             patch("app.services.github.GitHubService", return_value=mock_gh):
            mock_settings.fix_target_repo = "org/repo"
            await loop._check_merged_prs()

        mock_gh.is_pr_merged.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_github_api_error_leaves_incident_unchanged(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        incident = make_incident(IncidentStatus.AWAITING_APPROVAL)

        mock_store = MagicMock()
        mock_store.list_all = MagicMock(return_value=[incident])
        mock_store.update = MagicMock()

        mock_gh = AsyncMock()
        mock_gh.is_pr_merged = AsyncMock(side_effect=Exception("rate limited"))

        with patch("app.services.incident_loop.incident_store", mock_store), \
             patch("app.services.incident_loop.settings") as mock_settings, \
             patch("app.services.github.GitHubService", return_value=mock_gh):
            mock_settings.fix_target_repo = "org/repo"
            await loop._check_merged_prs()

        assert incident.status == IncidentStatus.AWAITING_APPROVAL
        mock_store.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_waiting_incidents_skips_github_init(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)

        mock_store = MagicMock()
        mock_store.list_all = MagicMock(return_value=[])

        with patch("app.services.incident_loop.incident_store", mock_store), \
             patch("app.services.github.GitHubService") as mock_gh_cls:
            await loop._check_merged_prs()

        mock_gh_cls.assert_not_called()
