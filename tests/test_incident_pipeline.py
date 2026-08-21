"""
End-to-end pipeline tests for the incident response loop.

Tests the full sequential chain:
  ErrorEvent → Triage → Diagnosis → Fix → Code Review → Human Approval

All agents, GitHub, AWS, and LLM calls are mocked — no network or API keys required.

Run:
    pytest tests/test_incident_pipeline.py -v
"""
from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.diagnosis import DiagnosisResult
from app.agents.fix_generation import FixResult
from app.agents.triage import TriageResult
from app.models.events import ErrorEvent, EventSource, IncidentStatus
from app.services.incident_store import IncidentStore

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

def make_error_event(**kwargs) -> ErrorEvent:
    defaults = dict(
        source=EventSource.APPLICATION,
        error_type="S3_NO_SUCH_KEY",
        title="NoSuchKey in moveAndRemoveFileFromS3",
        description="S3 throws NoSuchKey on missing key",
        service="image-service",
        metadata={"log_group": "/ecs/image-service", "pattern": "NoSuchKey"},
    )
    defaults.update(kwargs)
    return ErrorEvent(**defaults)


def make_triage_result(decision="real", severity="P2") -> TriageResult:
    return TriageResult(
        decision=decision,
        severity=severity,
        blast_radius="single_service",
        occurrences_24h=47,
        duplicate_pr=None,
        reasoning="47 occurrences, single service impacted",
    )


def make_diagnosis_result(confidence=0.85) -> DiagnosisResult:
    return DiagnosisResult(
        root_cause="S3 copy/delete attempted on key that no longer exists — no existence check",
        confidence=confidence,
        evidence=[
            "47 NoSuchKey errors in CloudWatch over 24h",
            "moveAndRemoveFileFromS3 in routes/services/image.js has no error handling",
        ],
        fix_approach="Wrap S3 ops in try/catch, catch NoSuchKey, return early",
        affected_function="moveAndRemoveFileFromS3",
        affected_file="routes/services/image.js",
        reproduction_confirmed=True,
        escalate=confidence < 0.70,
    )


def make_fix_result() -> FixResult:
    return FixResult(
        issue_url="https://github.com/org/repo/issues/10",
        pr_url="https://github.com/org/repo/pull/11",
        pr_number=11,
        branch="fix/s3-no-such-key-abc12345",
        fix_description="NoSuchKey try/catch added to routes/services/image.js",
        files_changed=["routes/services/image.js", "tests/services/image.test.js"],
        test_added=True,
        commit_sha="deadbeef12345678",
    )


# ---------------------------------------------------------------------------
# IncidentLoop pipeline tests
# ---------------------------------------------------------------------------

class TestIncidentPipeline:
    """Full pipeline — mocks all external I/O."""

    def _make_loop(self, *, triage_result, diagnosis_result, fix_result, review_text="Review posted."):
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        loop._running = False
        loop._rag = None
        loop._dedup_stats = {"sql_dedup": 0, "regression": 0, "rag_hit": 0, "cold_start": 0}

        triage_agent = MagicMock()
        triage_agent.triage = AsyncMock(return_value=triage_result)

        diagnosis_agent = MagicMock()
        diagnosis_agent.diagnose = AsyncMock(return_value=diagnosis_result)

        fix_agent = MagicMock()
        # _run_fix calls fix_with_steps, not fix; mock it to return (fix_result, steps)
        fix_agent.fix_with_steps = AsyncMock(return_value=(fix_result, []))

        review_agent = MagicMock()
        from app.agents.base import AgentResult
        review_agent.run = AsyncMock(return_value=AgentResult(
            answer=review_text, steps=[], iterations=3
        ))

        loop._triage = triage_agent
        loop._diagnosis = diagnosis_agent
        loop._fix_agent = fix_agent
        loop._review_agent = review_agent

        return loop

    @pytest.mark.asyncio
    async def test_real_incident_reaches_awaiting_approval(self):
        """Happy path: real incident runs all 5 steps, ends at AWAITING_APPROVAL."""
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}

        loop = self._make_loop(
            triage_result=make_triage_result("real"),
            diagnosis_result=make_diagnosis_result(0.85),
            fix_result=make_fix_result(),
        )

        with (
            patch("app.services.incident_loop.incident_store", store),
            patch("app.services.incident_loop.alerting_service") as mock_alert,
            patch("app.services.incident_loop.approval_service") as mock_approval,
        ):
            mock_alert.send_alert = AsyncMock()
            mock_approval.request_approval = AsyncMock(return_value=MagicMock(id="appr-001"))

            event = make_error_event()
            await loop._process(event)

        # One incident was created
        assert len(store._incidents) == 1
        incident = list(store._incidents.values())[0]

        assert incident.status == IncidentStatus.AWAITING_APPROVAL
        assert incident.triage_decision == "real"
        assert incident.confidence == pytest.approx(0.85)
        assert incident.reproduction_confirmed is True
        assert incident.pr_url == "https://github.com/org/repo/pull/11"
        assert incident.pr_number == 11
        assert incident.review_posted is True
        assert incident.approval_id == "appr-001"
        assert incident.fix_attempted is not None

        # PR is registered for idempotency using the full dedup key
        dedup_key = "S3_NO_SUCH_KEY:image-service:S3 throws NoSuchKey on missing key"
        assert store.get_pr_for_resource(dedup_key) == "https://github.com/org/repo/pull/11"

    @pytest.mark.asyncio
    async def test_restart_notes_metadata_becomes_human_notes(self):
        """restart_incident() (routes/incidents.py) carries operator notes forward
        via event.metadata["restart_notes"] rather than mutating an existing
        incident in place (see that endpoint's docstring for why). _process()
        must surface them as incident.human_notes the same way a real human note
        would be, so FixGenerationAgent's prompt actually sees them."""
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}

        loop = self._make_loop(
            triage_result=make_triage_result("real"),
            diagnosis_result=make_diagnosis_result(0.85),
            fix_result=make_fix_result(),
        )

        with (
            patch("app.services.incident_loop.incident_store", store),
            patch("app.services.incident_loop.alerting_service") as mock_alert,
            patch("app.services.incident_loop.approval_service") as mock_approval,
        ):
            mock_alert.send_alert = AsyncMock()
            mock_approval.request_approval = AsyncMock(return_value=MagicMock(id="appr-001"))

            event = make_error_event(metadata={"restarted": True, "restart_notes": "please also handle nulls"})
            await loop._process(event)

        incident = list(store._incidents.values())[0]
        assert incident.human_notes == "please also handle nulls"

    @pytest.mark.asyncio
    async def test_noise_event_terminates_at_triage(self):
        """Noise events are terminated without running diagnosis or fix."""
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}

        loop = self._make_loop(
            triage_result=make_triage_result("noise"),
            diagnosis_result=make_diagnosis_result(),  # should not be called
            fix_result=make_fix_result(),              # should not be called
        )

        with (
            patch("app.services.incident_loop.incident_store", store),
            patch("app.services.incident_loop.alerting_service") as mock_alert,
            patch("app.services.incident_loop.approval_service") as mock_approval,
        ):
            mock_alert.send_alert = AsyncMock()
            mock_approval.request_approval = AsyncMock()

            await loop._process(make_error_event())

        incident = list(store._incidents.values())[0]
        assert incident.status == IncidentStatus.NOISE
        loop._diagnosis.diagnose.assert_not_called()
        loop._fix_agent.fix.assert_not_called()
        mock_approval.request_approval.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_event_terminates_at_triage(self):
        """Duplicate events are terminated and link to existing PR."""
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}

        dup_result = TriageResult(
            decision="duplicate",
            severity="P2",
            blast_radius="single_service",
            occurrences_24h=12,
            duplicate_pr="https://github.com/org/repo/pull/9",
            reasoning="PR #9 already covers this error type",
        )

        loop = self._make_loop(
            triage_result=dup_result,
            diagnosis_result=make_diagnosis_result(),
            fix_result=make_fix_result(),
        )

        with (
            patch("app.services.incident_loop.incident_store", store),
            patch("app.services.incident_loop.alerting_service") as mock_alert,
            patch("app.services.incident_loop.approval_service") as mock_approval,
        ):
            mock_alert.send_alert = AsyncMock()
            mock_approval.request_approval = AsyncMock()

            await loop._process(make_error_event())

        incident = list(store._incidents.values())[0]
        assert incident.status == IncidentStatus.DUPLICATE
        assert incident.pr_url == "https://github.com/org/repo/pull/9"
        loop._diagnosis.diagnose.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_confidence_escalates_to_human(self):
        """Diagnosis below threshold escalates without running fix generation."""
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}

        loop = self._make_loop(
            triage_result=make_triage_result("real"),
            diagnosis_result=make_diagnosis_result(confidence=0.45),  # below 0.70 threshold
            fix_result=make_fix_result(),
        )

        with (
            patch("app.services.incident_loop.incident_store", store),
            patch("app.services.incident_loop.alerting_service") as mock_alert,
            patch("app.services.incident_loop.approval_service") as mock_approval,
        ):
            mock_alert.send_alert = AsyncMock()
            mock_approval.request_approval = AsyncMock()

            await loop._process(make_error_event())

        incident = list(store._incidents.values())[0]
        assert incident.status == IncidentStatus.AWAITING_APPROVAL
        assert incident.confidence == pytest.approx(0.45)
        # fix_with_steps should not be called — low confidence blocks fix generation
        loop._fix_agent.fix_with_steps.assert_not_called()
        # request_approval IS called once (diagnosis escalation to human)
        mock_approval.request_approval.assert_called_once()

    @pytest.mark.asyncio
    async def test_fix_failure_marks_incident_fix_failed(self):
        """If fix generation raises, incident moves to the dedicated FIX_FAILED
        status (not left in FIXING) so it's distinguishable and escalated,
        rather than looking indistinguishable from a fix still in progress."""
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}

        loop = self._make_loop(
            triage_result=make_triage_result("real"),
            diagnosis_result=make_diagnosis_result(0.85),
            fix_result=make_fix_result(),
        )
        loop._fix_agent.fix_with_steps = AsyncMock(side_effect=RuntimeError("GitHub API timeout"))

        with (
            patch("app.services.incident_loop.incident_store", store),
            patch("app.services.incident_loop.alerting_service") as mock_alert,
            patch("app.services.incident_loop.approval_service") as mock_approval,
        ):
            mock_alert.send_alert = AsyncMock()
            mock_approval.request_approval = AsyncMock()

            await loop._process(make_error_event())

        incident = list(store._incidents.values())[0]
        assert incident.status == IncidentStatus.FIX_FAILED
        mock_approval.request_approval.assert_not_called()

    @pytest.mark.asyncio
    async def test_triage_failure_defaults_to_real(self):
        """TriageAgent error → defaults to real/P2, pipeline continues."""
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}

        loop = self._make_loop(
            triage_result=make_triage_result("real"),
            diagnosis_result=make_diagnosis_result(0.85),
            fix_result=make_fix_result(),
        )
        loop._triage.triage = AsyncMock(side_effect=RuntimeError("Haiku timeout"))

        with (
            patch("app.services.incident_loop.incident_store", store),
            patch("app.services.incident_loop.alerting_service") as mock_alert,
            patch("app.services.incident_loop.approval_service") as mock_approval,
        ):
            mock_alert.send_alert = AsyncMock()
            mock_approval.request_approval = AsyncMock(return_value=MagicMock(id="appr-002"))

            await loop._process(make_error_event())

        incident = list(store._incidents.values())[0]
        # Fallback triage → real/P2 → pipeline continues
        assert incident.triage_decision == "real"
        assert incident.status == IncidentStatus.AWAITING_APPROVAL


# ---------------------------------------------------------------------------
# Approval → Incident resolution
# ---------------------------------------------------------------------------

class TestApprovalResolution:
    """Approval decisions close the loop on incident state."""

    @pytest.mark.asyncio
    async def test_approve_records_human_decision_without_resolving(self):
        """Approving a merge_ai_fix_pr request records human_decision but does
        NOT resolve the incident immediately -- resolution waits for
        _check_merged_prs to confirm via GitHub that the PR actually merged
        (see app/api/routes/approvals.py::approve), so an approval click
        alone can't produce a false "resolved" if the merge itself fails."""
        from app.models.events import IncidentStatus
        from app.services.approvals import ApprovalRequest, ApprovalStatus, RiskLevel, _store

        # Seed an incident in AWAITING_APPROVAL
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}
        event = make_error_event()
        incident = store.create(event)
        incident.status = IncidentStatus.AWAITING_APPROVAL
        incident.pr_url = "https://github.com/org/repo/pull/11"
        incident.pr_number = 11
        store.update(incident)

        # Seed the approval request pointing at this incident
        approval = ApprovalRequest(
            agent_name="FixGenerationAgent",
            action="merge_ai_fix_pr",
            parameters={"incident_id": incident.id, "pr_number": 11},
            risk_level=RiskLevel.HIGH,
            description="Merge AI fix",
        )
        _store[approval.id] = approval

        with (
            patch("app.api.routes.approvals.incident_store", store),
            patch("app.api.routes.approvals.approval_service") as mock_svc,
        ):
            mock_svc.approve.return_value = approval
            approval.status = ApprovalStatus.APPROVED
            approval.decided_by = "siddharth"

            from app.api.routes.approvals import ApproveBody, approve
            await approve(approval.id, ApproveBody(approver="siddharth"))

        assert incident.status == IncidentStatus.AWAITING_APPROVAL
        assert incident.human_decision == "approved"
        assert incident.outcome is None
        assert incident.resolved_at is None

    @pytest.mark.asyncio
    async def test_approve_diagnosis_escalation_with_notes_sets_human_notes(self):
        """Approving a low-confidence diagnosis escalation with notes threads
        them into incident.human_notes (the same mechanism refix-with-notes
        uses to inject human feedback into FixGenerationAgent's prompt)
        before resume_fix runs -- lets a human correct/steer a diagnosis they
        agree is directionally right but suspect is partly fabricated,
        instead of only being able to say yes/no."""
        from app.models.events import IncidentStatus
        from app.services.approvals import ApprovalRequest, ApprovalStatus, RiskLevel, _store

        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}
        event = make_error_event()
        incident = store.create(event)
        incident.status = IncidentStatus.AWAITING_APPROVAL
        store.update(incident)

        approval = ApprovalRequest(
            agent_name="DiagnosisAgent",
            action="approve_diagnosis_escalation",
            parameters={"incident_id": incident.id},
            risk_level=RiskLevel.HIGH,
            description="Low-confidence diagnosis",
        )
        _store[approval.id] = approval

        mock_resume_fix = AsyncMock()
        with (
            patch("app.api.routes.approvals.incident_store", store),
            patch("app.api.routes.approvals.approval_service") as mock_svc,
            patch("app.services.incident_loop.incident_loop") as mock_loop,
        ):
            mock_svc.approve.return_value = approval
            approval.status = ApprovalStatus.APPROVED
            approval.decided_by = "siddharth"
            mock_loop.resume_fix = mock_resume_fix

            from app.api.routes.approvals import ApproveBody, approve
            await approve(
                approval.id,
                ApproveBody(
                    approver="siddharth",
                    notes="diagnosis's file list looks fabricated -- only models/PrankCheckerLog.js is real",
                ),
            )

        updated = store.get(incident.id)
        assert updated.human_notes is not None
        assert "HUMAN INSTRUCTION:" in updated.human_notes
        assert "models/PrankCheckerLog.js" in updated.human_notes
        mock_resume_fix.assert_called_once_with(incident.id)

    @pytest.mark.asyncio
    async def test_reject_marks_incident_rejected(self):
        from app.services.approvals import ApprovalRequest, ApprovalStatus, RiskLevel, _store

        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}
        event = make_error_event()
        incident = store.create(event)
        incident.status = IncidentStatus.AWAITING_APPROVAL
        store.update(incident)

        approval = ApprovalRequest(
            agent_name="FixGenerationAgent",
            action="merge_ai_fix_pr",
            parameters={"incident_id": incident.id},
            risk_level=RiskLevel.HIGH,
            description="Merge AI fix",
        )
        _store[approval.id] = approval

        with (
            patch("app.api.routes.approvals.incident_store", store),
            patch("app.api.routes.approvals.approval_service") as mock_svc,
        ):
            mock_svc.reject.return_value = approval
            approval.status = ApprovalStatus.REJECTED
            approval.decided_by = "siddharth"
            approval.rejection_reason = "Fix too aggressive — try scoping to NoSuchKey only"

            from app.api.routes.approvals import RejectBody, reject
            await reject(
                approval.id,
                RejectBody(approver="siddharth", reason="Fix too aggressive — try scoping to NoSuchKey only"),
            )

        assert incident.status == IncidentStatus.REJECTED
        assert incident.human_decision == "rejected"
        assert incident.outcome == "fix_rejected"
        assert incident.human_decision_reason == "Fix too aggressive — try scoping to NoSuchKey only"
        assert incident.resolved_at is not None

    @pytest.mark.asyncio
    async def test_approve_without_incident_id_is_safe(self):
        """Approval with no incident_id param does not crash."""
        from app.services.approvals import ApprovalRequest, ApprovalStatus, RiskLevel

        approval = ApprovalRequest(
            agent_name="SomeAgent",
            action="some_action",
            parameters={},  # no incident_id
            risk_level=RiskLevel.HIGH,
            description="Some action",
        )
        approval.status = ApprovalStatus.APPROVED
        approval.decided_by = "siddharth"

        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}

        with (
            patch("app.api.routes.approvals.incident_store", store),
            patch("app.api.routes.approvals.approval_service") as mock_svc,
        ):
            mock_svc.approve.return_value = approval
            from app.api.routes.approvals import ApproveBody, approve
            result = await approve(approval.id, ApproveBody(approver="siddharth"))

        # No incident updated, no crash
        assert result is approval


# ---------------------------------------------------------------------------
# Trigger endpoint
# ---------------------------------------------------------------------------

class TestTriggerEndpoint:
    @pytest.mark.asyncio
    async def test_trigger_enqueues_event(self):
        from app.api.routes.incidents import TriggerBody, trigger_incident

        with patch("app.api.routes.incidents.event_queue") as mock_queue:
            mock_queue.enqueue = AsyncMock()
            body = TriggerBody(
                error_type="S3_NO_SUCH_KEY",
                title="Test trigger",
                service="image-service",
            )
            result = await trigger_incident(body)

        assert result["status"] == "queued"
        assert result["title"] == "Test trigger"
        assert "event_id" in result
        mock_queue.enqueue.assert_called_once()
        enqueued_event = mock_queue.enqueue.call_args[0][0]
        assert enqueued_event.error_type == "S3_NO_SUCH_KEY"
        assert enqueued_event.service == "image-service"

    @pytest.mark.asyncio
    async def test_trigger_with_log_group(self):
        from app.api.routes.incidents import TriggerBody, trigger_incident

        with patch("app.api.routes.incidents.event_queue") as mock_queue:
            mock_queue.enqueue = AsyncMock()
            body = TriggerBody(log_group="/ecs/image-service")
            await trigger_incident(body)

        enqueued_event = mock_queue.enqueue.call_args[0][0]
        assert enqueued_event.metadata["log_group"] == "/ecs/image-service"


# ---------------------------------------------------------------------------
# IncidentState model tests
# ---------------------------------------------------------------------------

class TestIncidentStateModel:
    def test_new_fields_default_correctly(self):
        event = make_error_event()
        from app.services.incident_store import IncidentStore
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}
        incident = store.create(event)

        assert incident.pr_number is None
        assert incident.approval_id is None
        assert incident.review_posted is False
        assert incident.human_decision is None
        assert incident.outcome is None

    def test_rejected_status_exists(self):
        assert IncidentStatus.REJECTED == "rejected"

    def test_mttr_tracked_on_rejection(self):
        event = make_error_event()
        from app.services.incident_store import IncidentStore
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}
        incident = store.create(event)

        import time
        time.sleep(0.01)
        incident.resolved_at = datetime.utcnow()
        incident.status = IncidentStatus.REJECTED

        assert incident.mttr_seconds is not None
        assert incident.mttr_seconds > 0

    def test_full_state_serializes(self):
        """All new fields round-trip through model_dump → model_validate."""
        event = make_error_event()
        from app.services.incident_store import IncidentStore
        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}
        incident = store.create(event)

        incident.pr_number = 42
        incident.approval_id = "appr-abc"
        incident.review_posted = True
        incident.human_decision = "approved"
        incident.outcome = "fix_merged"
        incident.status = IncidentStatus.RESOLVED
        incident.resolved_at = datetime.utcnow()

        from app.models.events import IncidentState
        dumped = incident.model_dump(mode="json")
        restored = IncidentState.model_validate(dumped)

        assert restored.pr_number == 42
        assert restored.approval_id == "appr-abc"
        assert restored.review_posted is True
        assert restored.status == IncidentStatus.RESOLVED
