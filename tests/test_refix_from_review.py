"""
Tests for the "re-run fix with code review feedback" feature.

Covers:
  - REQUEST_CHANGES from CodeReviewAgent routes incident to AWAITING_REFIX_APPROVAL
  - human_notes set to review text on REQUEST_CHANGES
  - POST /incidents/{id}/refix triggers refix_from_review
  - POST /incidents/{id}/reject-refix marks incident REJECTED
  - refix_from_review: old PR closed, fix re-runs, DoD gate, REVIEWING transition
  - APPROVE and NEEDS_DISCUSSION reviews do NOT divert to AWAITING_REFIX_APPROVAL
  - _extract_review_recommendation parsing

Run:
    pytest tests/test_refix_from_review.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.fix_generation import FixResult
from app.models.events import ErrorEvent, EventSource, IncidentState, IncidentStatus
from app.services.incident_loop import _extract_review_recommendation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_incident(**kwargs) -> IncidentState:
    event = ErrorEvent(
        source=EventSource.APPLICATION,
        error_type="NULL_POINTER",
        title="NPE in processOrder",
        description="NullPointerException thrown at processOrder line 42",
        service="order-service",
    )
    incident = IncidentState(error_event=event)
    incident.confidence = 0.85
    incident.occurrences_24h = 12
    for k, v in kwargs.items():
        setattr(incident, k, v)
    return incident


def make_fix_result(**kwargs) -> FixResult:
    defaults = dict(
        issue_url="https://github.com/org/repo/issues/20",
        pr_url="https://github.com/org/repo/pull/21",
        pr_number=21,
        branch="fix/null-pointer-abc",
        fix_description="Add null check before processOrder",
        files_changed=["src/orders.py", "tests/test_orders.py"],
        test_added=True,
        commit_sha="cafebabe",
    )
    defaults.update(kwargs)
    return FixResult(**defaults)


REVIEW_REQUEST_CHANGES = """\
## Code Review

The fix adds a null check but the logic appears inverted — `if obj is not None` should
be `if obj is None` to guard the early return.

## Recommendation
REQUEST_CHANGES

**Rationale:** Test logic is inverted; fix would break non-null cases.
"""

REVIEW_APPROVE = """\
## Code Review

Looks correct. The null guard is properly placed.

## Recommendation
APPROVE

**Rationale:** Correct fix, test covers the new path.
"""


# ---------------------------------------------------------------------------
# _extract_review_recommendation
# ---------------------------------------------------------------------------

class TestExtractReviewRecommendation:
    def test_request_changes(self):
        assert _extract_review_recommendation(REVIEW_REQUEST_CHANGES) == "REQUEST_CHANGES"

    def test_approve(self):
        assert _extract_review_recommendation(REVIEW_APPROVE) == "APPROVE"

    def test_needs_discussion(self):
        text = "## Recommendation\nNEEDS_DISCUSSION\n\nRationale: unclear scope."
        assert _extract_review_recommendation(text) == "NEEDS_DISCUSSION"

    def test_empty_defaults_to_approve(self):
        assert _extract_review_recommendation("") == "APPROVE"

    def test_no_keyword_defaults_to_approve(self):
        assert _extract_review_recommendation("looks fine to me") == "APPROVE"


# ---------------------------------------------------------------------------
# _run_post_fix — REQUEST_CHANGES routing
# ---------------------------------------------------------------------------

class TestRunPostFixRequestChanges:
    @pytest.mark.asyncio
    async def test_request_changes_sets_awaiting_refix_approval(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        store = MagicMock()
        incident = make_incident(
            status=IncidentStatus.REVIEWING,
            pr_url="https://github.com/org/repo/pull/21",
            pr_number=21,
        )
        fix = make_fix_result()

        loop._run_review = AsyncMock(return_value=REVIEW_REQUEST_CHANGES)

        with patch("app.services.incident_loop.incident_store", store), \
             patch("app.services.incident_loop._notify_refix_approval_needed", AsyncMock()) as mock_notify:
            await loop._run_post_fix(incident, fix)

        assert incident.status == IncidentStatus.AWAITING_REFIX_APPROVAL
        assert incident.human_notes == REVIEW_REQUEST_CHANGES
        mock_notify.assert_awaited_once()
        store.update.assert_called()

    @pytest.mark.asyncio
    async def test_approve_creates_merge_approval(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        store = MagicMock()
        incident = make_incident(
            status=IncidentStatus.REVIEWING,
            pr_url="https://github.com/org/repo/pull/21",
            pr_number=21,
        )
        fix = make_fix_result()

        loop._run_review = AsyncMock(return_value=REVIEW_APPROVE)

        mock_approval_req = MagicMock()
        mock_approval_req.id = "approval-abc"

        with patch("app.services.incident_loop.incident_store", store), \
             patch("app.services.incident_loop.approval_service") as mock_approvals, \
             patch("app.services.incident_loop._notify_fix_ready", AsyncMock()):
            mock_approvals.request_approval = AsyncMock(return_value=mock_approval_req)
            await loop._run_post_fix(incident, fix)

        assert incident.status == IncidentStatus.AWAITING_APPROVAL
        assert incident.approval_id == "approval-abc"

    @pytest.mark.asyncio
    async def test_no_review_text_falls_through_to_merge_approval(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        store = MagicMock()
        incident = make_incident(
            status=IncidentStatus.REVIEWING,
            pr_url="https://github.com/org/repo/pull/21",
            pr_number=21,
        )
        fix = make_fix_result()

        loop._run_review = AsyncMock(return_value=None)

        mock_approval_req = MagicMock()
        mock_approval_req.id = "approval-xyz"

        with patch("app.services.incident_loop.incident_store", store), \
             patch("app.services.incident_loop.approval_service") as mock_approvals, \
             patch("app.services.incident_loop._notify_fix_ready", AsyncMock()):
            mock_approvals.request_approval = AsyncMock(return_value=mock_approval_req)
            await loop._run_post_fix(incident, fix)

        assert incident.status == IncidentStatus.AWAITING_APPROVAL


# ---------------------------------------------------------------------------
# refix_from_review
# ---------------------------------------------------------------------------

class TestRefixFromReview:
    @pytest.mark.asyncio
    async def test_happy_path_closes_old_pr_and_reruns_fix(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        incident = make_incident(
            status=IncidentStatus.AWAITING_REFIX_APPROVAL,
            pr_url="https://github.com/org/repo/pull/10",
            pr_number=10,
            human_notes=REVIEW_REQUEST_CHANGES,
        )
        new_fix = make_fix_result(pr_url="https://github.com/org/repo/pull/21", pr_number=21)

        store = MagicMock()
        store.get = MagicMock(return_value=incident)

        mock_gh = AsyncMock()
        loop._run_fix = AsyncMock(return_value=new_fix)
        loop._run_post_fix = AsyncMock()

        with patch("app.services.incident_loop.incident_store", store), \
             patch("app.services.incident_loop.session_logger") as mock_sess, \
             patch("app.services.incident_loop._apply_dod_gate", AsyncMock(return_value=True)), \
             patch("app.services.incident_loop.settings") as mock_settings, \
             patch("app.services.github.GitHubService", return_value=mock_gh):
            mock_settings.fix_target_repo = "org/repo"
            mock_sess.get = MagicMock(return_value=MagicMock())
            await loop.refix_from_review(incident.id)

        mock_gh.close_pull_request.assert_awaited_once_with("org", "repo", 10)
        loop._run_fix.assert_awaited_once()
        loop._run_post_fix.assert_awaited_once()
        assert incident.status == IncidentStatus.REVIEWING
        assert incident.pr_url == new_fix.pr_url

    @pytest.mark.asyncio
    async def test_wrong_status_is_a_noop(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        incident = make_incident(status=IncidentStatus.REVIEWING)

        store = MagicMock()
        store.get = MagicMock(return_value=incident)
        loop._run_fix = AsyncMock()

        with patch("app.services.incident_loop.incident_store", store):
            await loop.refix_from_review(incident.id)

        loop._run_fix.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fix_failure_leaves_incident_with_fix_failed_session(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        incident = make_incident(status=IncidentStatus.AWAITING_REFIX_APPROVAL)

        store = MagicMock()
        store.get = MagicMock(return_value=incident)
        loop._run_fix = AsyncMock(return_value=None)

        with patch("app.services.incident_loop.incident_store", store), \
             patch("app.services.incident_loop.session_logger") as mock_sess, \
             patch("app.services.incident_loop.settings") as mock_settings:
            mock_settings.fix_target_repo = "org/repo"
            mock_sess.get = MagicMock(return_value=MagicMock())
            incident.pr_number = None  # no old PR to close
            await loop.refix_from_review(incident.id)

        mock_sess.finish.assert_called_once_with(incident.id, "fix_failed")

    @pytest.mark.asyncio
    async def test_dod_gate_failure_leaves_verification_failed(self):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        incident = make_incident(status=IncidentStatus.AWAITING_REFIX_APPROVAL)
        incident.pr_number = None
        new_fix = make_fix_result()

        store = MagicMock()
        store.get = MagicMock(return_value=incident)
        loop._run_fix = AsyncMock(return_value=new_fix)
        loop._run_post_fix = AsyncMock()

        with patch("app.services.incident_loop.incident_store", store), \
             patch("app.services.incident_loop.session_logger") as mock_sess, \
             patch("app.services.incident_loop._apply_dod_gate", AsyncMock(return_value=False)), \
             patch("app.services.incident_loop.settings") as mock_settings:
            mock_settings.fix_target_repo = "org/repo"
            mock_sess.get = MagicMock(return_value=MagicMock())
            await loop.refix_from_review(incident.id)

        loop._run_post_fix.assert_not_awaited()
        mock_sess.finish.assert_called_once_with(incident.id, "dod_failed")


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

class TestRefixEndpoints:
    @pytest.mark.asyncio
    async def test_refix_endpoint_queues_refix(self):
        from fastapi.testclient import TestClient

        from app.main import app

        incident = make_incident(status=IncidentStatus.AWAITING_REFIX_APPROVAL)
        mock_loop = MagicMock()
        mock_loop.refix_from_review = AsyncMock()

        with patch("app.api.routes.incidents.incident_store") as mock_store, \
             patch("app.services.incident_loop.incident_loop", mock_loop):
            mock_store.get = MagicMock(return_value=incident)
            mock_store.update = MagicMock()

            client = TestClient(app)
            resp = client.post(f"/incidents/{incident.id}/refix")

        assert resp.status_code == 200
        assert resp.json()["status"] == "refix_queued"

    @pytest.mark.asyncio
    async def test_refix_endpoint_rejects_wrong_status(self):
        from fastapi.testclient import TestClient

        from app.main import app

        incident = make_incident(status=IncidentStatus.REVIEWING)

        with patch("app.api.routes.incidents.incident_store") as mock_store:
            mock_store.get = MagicMock(return_value=incident)
            client = TestClient(app)
            resp = client.post(f"/incidents/{incident.id}/refix")

        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_reject_refix_marks_rejected(self):
        from fastapi.testclient import TestClient

        from app.main import app

        incident = make_incident(status=IncidentStatus.AWAITING_REFIX_APPROVAL)

        with patch("app.api.routes.incidents.incident_store") as mock_store:
            mock_store.get = MagicMock(return_value=incident)
            mock_store.update = MagicMock()
            client = TestClient(app)
            resp = client.post(f"/incidents/{incident.id}/reject-refix")

        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        assert incident.status == IncidentStatus.REJECTED

    @pytest.mark.asyncio
    async def test_reject_refix_rejects_wrong_status(self):
        from fastapi.testclient import TestClient

        from app.main import app

        incident = make_incident(status=IncidentStatus.REVIEWING)

        with patch("app.api.routes.incidents.incident_store") as mock_store:
            mock_store.get = MagicMock(return_value=incident)
            client = TestClient(app)
            resp = client.post(f"/incidents/{incident.id}/reject-refix")

        assert resp.status_code == 400
