"""
Tests for PreferenceLogger — RLHF rejection pair logging.

Run:
    pytest tests/test_preference_logger.py -v
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from app.services.preference_logger import PreferenceLogger, preference_logger


# ---------------------------------------------------------------------------
# Helpers — build minimal IncidentState
# ---------------------------------------------------------------------------

def _make_incident(
    *,
    error_type="S3_NO_SUCH_KEY",
    service="image-service",
    diagnosis="Missing S3 key in bucket lookup",
    confidence=0.85,
    triage_decision="real",
    triage_reasoning="Recurring error, definitely real",
    blast_radius="single_service",
    pr_url="https://github.com/org/repo/pull/42",
    pr_number=42,
    fix_description="Modified s3_utils.py line 87 to add key existence check",
    fix_attempted=None,
):
    from app.models.events import ErrorEvent, EventSource, IncidentState

    event = ErrorEvent(
        source=EventSource.APPLICATION,
        error_type=error_type,
        title=f"{error_type} in {service}",
        description="test",
        service=service,
    )
    inc = IncidentState(error_event=event)
    inc.diagnosis = diagnosis
    inc.confidence = confidence
    inc.triage_decision = triage_decision
    inc.triage_reasoning = triage_reasoning
    inc.blast_radius = blast_radius
    inc.pr_url = pr_url
    inc.pr_number = pr_number
    inc.fix_description = fix_description
    inc.fix_attempted = fix_attempted or (fix_description[:200] if fix_description else None)
    return inc


# ---------------------------------------------------------------------------
# PreferenceLogger.log_rejection — pair structure
# ---------------------------------------------------------------------------

class TestLogRejectionStructure:
    def test_returns_dict(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        inc = _make_incident()
        pair = pl.log_rejection(inc, approver="alice", reason="wrong function")
        assert isinstance(pair, dict)

    def test_id_starts_with_pref(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pair = pl.log_rejection(_make_incident(), "alice", "reason")
        assert pair["id"].startswith("pref_")

    def test_type_is_rejection(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pair = pl.log_rejection(_make_incident(), "alice", "reason")
        assert pair["type"] == "rejection"

    def test_timestamp_is_iso_format(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pair = pl.log_rejection(_make_incident(), "alice", "reason")
        # Should parse without error
        datetime.fromisoformat(pair["timestamp"])

    def test_incident_id_recorded(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        inc = _make_incident()
        pair = pl.log_rejection(inc, "alice", "reason")
        assert pair["incident_id"] == inc.id

    def test_error_type_and_service(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pair = pl.log_rejection(_make_incident(), "alice", "reason")
        assert pair["error_type"] == "S3_NO_SUCH_KEY"
        assert pair["service"] == "image-service"


class TestLogRejectionPrompt:
    def test_diagnosis_captured(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pair = pl.log_rejection(_make_incident(), "alice", "reason")
        assert pair["prompt"]["diagnosis"] == "Missing S3 key in bucket lookup"

    def test_confidence_captured(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pair = pl.log_rejection(_make_incident(), "alice", "reason")
        assert pair["prompt"]["confidence"] == 0.85

    def test_triage_fields_captured(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pair = pl.log_rejection(_make_incident(), "alice", "reason")
        assert pair["prompt"]["triage_decision"] == "real"
        assert "Recurring error" in pair["prompt"]["triage_reasoning"]
        assert pair["prompt"]["blast_radius"] == "single_service"


class TestLogRejectionResponse:
    def test_pr_url_and_number_captured(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pair = pl.log_rejection(_make_incident(), "alice", "reason")
        assert pair["rejected_response"]["pr_url"] == "https://github.com/org/repo/pull/42"
        assert pair["rejected_response"]["pr_number"] == 42

    def test_fix_description_captured(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pair = pl.log_rejection(_make_incident(), "alice", "reason")
        assert "s3_utils.py" in pair["rejected_response"]["fix_description"]

    def test_falls_back_to_fix_attempted_when_no_description(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        inc = _make_incident(fix_description=None, fix_attempted="partial fix text")
        pair = pl.log_rejection(inc, "alice", "reason")
        assert pair["rejected_response"]["fix_description"] == "partial fix text"


class TestLogRejectionMeta:
    def test_reason_and_approver_captured(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pair = pl.log_rejection(_make_incident(), approver="bob", reason="touches auth")
        assert pair["rejection"]["reason"] == "touches auth"
        assert pair["rejection"]["approver"] == "bob"

    def test_counter_increments(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        assert pl.logged_count == 0
        pl.log_rejection(_make_incident(), "a", "r")
        assert pl.logged_count == 1
        pl.log_rejection(_make_incident(), "a", "r")
        assert pl.logged_count == 2


# ---------------------------------------------------------------------------
# JSONL persistence
# ---------------------------------------------------------------------------

class TestJSONLPersistence:
    def test_file_created_on_first_log(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        assert not (tmp_path / "pairs.jsonl").exists()
        pl.log_rejection(_make_incident(), "alice", "reason")
        assert (tmp_path / "pairs.jsonl").exists()

    def test_each_line_is_valid_json(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pl.log_rejection(_make_incident(), "a", "r1")
        pl.log_rejection(_make_incident(), "b", "r2")
        lines = (tmp_path / "pairs.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert "id" in obj

    def test_multiple_rejections_appended_not_overwritten(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        for i in range(5):
            pl.log_rejection(_make_incident(), "alice", f"reason {i}")
        lines = (tmp_path / "pairs.jsonl").read_text().strip().splitlines()
        assert len(lines) == 5

    def test_records_survive_new_logger_instance(self, tmp_path):
        path = tmp_path / "pairs.jsonl"
        pl1 = PreferenceLogger(path=path)
        pl1.log_rejection(_make_incident(), "alice", "reason")

        pl2 = PreferenceLogger(path=path)
        all_pairs = pl2.get_all()
        assert len(all_pairs) == 1
        assert all_pairs[0]["rejection"]["approver"] == "alice"


# ---------------------------------------------------------------------------
# get_all / get_rejections_for_incident
# ---------------------------------------------------------------------------

class TestGetAll:
    def test_empty_when_no_file(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        assert pl.get_all() == []

    def test_returns_all_pairs(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pl.log_rejection(_make_incident(), "a", "r1")
        pl.log_rejection(_make_incident(), "b", "r2")
        assert len(pl.get_all()) == 2

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "pairs.jsonl"
        path.write_text('{"id":"good"}\nnot-json\n{"id":"also-good"}\n')
        pl = PreferenceLogger(path=path)
        pairs = pl.get_all()
        assert len(pairs) == 2


class TestGetRejectionsForIncident:
    def test_filters_by_incident_id(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        inc1 = _make_incident()
        inc2 = _make_incident()
        pl.log_rejection(inc1, "a", "r1")
        pl.log_rejection(inc2, "b", "r2")
        pl.log_rejection(inc1, "c", "r3")

        results = pl.get_rejections_for_incident(inc1.id)
        assert len(results) == 2
        assert all(p["incident_id"] == inc1.id for p in results)

    def test_returns_empty_for_unknown_incident(self, tmp_path):
        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")
        pl.log_rejection(_make_incident(), "a", "r")
        assert pl.get_rejections_for_incident("non-existent-id") == []


# ---------------------------------------------------------------------------
# Approvals endpoint integration
# ---------------------------------------------------------------------------

class TestApprovalsEndpointIntegration:
    @pytest.mark.asyncio
    async def test_reject_endpoint_logs_preference_pair(self, tmp_path):
        """POST /approvals/{id}/reject triggers preference_logger.log_rejection."""
        from unittest.mock import MagicMock, patch

        from fastapi.testclient import TestClient

        from app.api.routes.approvals import router
        from app.models.events import IncidentStatus
        from app.services.approvals import ApprovalRequest, RiskLevel

        # Build a rejected approval request linked to an incident
        inc = _make_incident()
        req = ApprovalRequest(
            id="req_test",
            agent_name="IncidentResponseAgent",
            action="merge_pr",
            title="Fix for S3 error",
            description="",
            risk_level=RiskLevel.HIGH,
            parameters={"incident_id": inc.id},
        )
        req.status = "pending"

        pl = PreferenceLogger(path=tmp_path / "pairs.jsonl")

        with (
            patch("app.api.routes.approvals.approval_service") as mock_approval,
            patch("app.api.routes.approvals.incident_store") as mock_store,
            patch("app.api.routes.approvals.preference_logger", pl),
        ):
            mock_approval.reject.return_value = req
            req.status = "rejected"
            mock_approval.reject.return_value = req
            mock_store.get.return_value = inc

            from fastapi import FastAPI
            app = FastAPI()
            app.include_router(router)
            client = TestClient(app)

            response = client.post(
                "/approvals/req_test/reject",
                json={"approver": "alice", "reason": "wrong fix"},
            )

        assert response.status_code == 200
        pairs = pl.get_all()
        assert len(pairs) == 1
        assert pairs[0]["rejection"]["approver"] == "alice"
        assert pairs[0]["rejection"]["reason"] == "wrong fix"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_is_instance(self):
        assert isinstance(preference_logger, PreferenceLogger)
