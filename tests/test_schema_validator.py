"""
Tests for HandoffValidator — schema validation between agent handoffs.

Run:
    pytest tests/test_schema_validator.py -v
"""
from __future__ import annotations

import pytest

from app.services.schema_validator import (
    HandoffValidationError,
    HandoffValidator,
    SchemaViolation,
    handoff_validator,
)

# ---------------------------------------------------------------------------
# Helpers — minimal stand-ins for the real dataclasses
# ---------------------------------------------------------------------------

def _triage(
    decision="real",
    severity="P2",
    blast_radius="single_service",
    occurrences_24h=3,
    duplicate_pr=None,
    reasoning="Looks like a real error",
):
    from app.agents.triage import TriageResult
    return TriageResult(
        decision=decision,
        severity=severity,
        blast_radius=blast_radius,
        occurrences_24h=occurrences_24h,
        duplicate_pr=duplicate_pr,
        reasoning=reasoning,
    )


def _diagnosis(
    root_cause="Missing S3 key",
    confidence=0.85,
    escalate=False,
):
    from app.agents.diagnosis import DiagnosisResult
    return DiagnosisResult(root_cause=root_cause, confidence=confidence, escalate=escalate)


def _fix(pr_number=42, pr_url="https://github.com/org/repo/pull/42"):
    from app.agents.fix_generation import FixResult
    return FixResult(
        issue_url="https://github.com/org/repo/issues/1",
        pr_url=pr_url,
        pr_number=pr_number,
        branch="fix/s3-key",
        fix_description="Fixed the S3 key lookup",
    )


# ---------------------------------------------------------------------------
# SchemaViolation
# ---------------------------------------------------------------------------

class TestSchemaViolation:
    def test_str_includes_field_and_message(self):
        v = SchemaViolation("decision", "REAL", "must be lowercase")
        assert "decision" in str(v)
        assert "must be lowercase" in str(v)


# ---------------------------------------------------------------------------
# HandoffValidationError
# ---------------------------------------------------------------------------

class TestHandoffValidationError:
    def test_stage_and_violations_stored(self):
        v = SchemaViolation("confidence", 1.5, "out of range")
        exc = HandoffValidationError("diagnosis→incident", [v])
        assert exc.stage == "diagnosis→incident"
        assert exc.violations == [v]

    def test_message_includes_stage(self):
        v = SchemaViolation("x", None, "missing")
        exc = HandoffValidationError("triage→incident", [v])
        assert "triage→incident" in str(exc)

    def test_is_subclass_of_value_error(self):
        exc = HandoffValidationError("s", [])
        assert isinstance(exc, ValueError)


# ---------------------------------------------------------------------------
# validate_triage — happy path
# ---------------------------------------------------------------------------

class TestValidateTriageHappy:
    def test_valid_result_returned_unchanged(self):
        r = _triage()
        out = handoff_validator.validate_triage(r)
        assert out.decision == "real"
        assert out.severity == "P2"

    def test_all_valid_decisions_accepted(self):
        for d in ("real", "noise", "duplicate"):
            handoff_validator.validate_triage(_triage(decision=d))

    def test_all_valid_severities_accepted(self):
        for s in ("P0", "P1", "P2", "P3"):
            handoff_validator.validate_triage(_triage(severity=s))

    def test_all_valid_blast_radii_accepted(self):
        for br in ("single_service", "multi_service", "unknown"):
            handoff_validator.validate_triage(_triage(blast_radius=br))

    def test_zero_occurrences_accepted(self):
        handoff_validator.validate_triage(_triage(occurrences_24h=0))


# ---------------------------------------------------------------------------
# validate_triage — coercions
# ---------------------------------------------------------------------------

class TestValidateTriageCoercions:
    def test_uppercase_decision_coerced(self):
        out = handoff_validator.validate_triage(_triage(decision="REAL"))
        assert out.decision == "real"

    def test_mixed_case_decision_coerced(self):
        out = handoff_validator.validate_triage(_triage(decision="Noise"))
        assert out.decision == "noise"

    def test_lowercase_severity_coerced(self):
        out = handoff_validator.validate_triage(_triage(severity="p1"))
        assert out.severity == "P1"

    def test_mixed_case_blast_radius_coerced(self):
        out = handoff_validator.validate_triage(_triage(blast_radius="Single_Service"))
        assert out.blast_radius == "single_service"

    def test_whitespace_stripped_from_decision(self):
        out = handoff_validator.validate_triage(_triage(decision="  real  "))
        assert out.decision == "real"


# ---------------------------------------------------------------------------
# validate_triage — violations
# ---------------------------------------------------------------------------

class TestValidateTriageViolations:
    def test_unknown_decision_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_triage(_triage(decision="maybe"))
        assert exc_info.value.stage == "triage→incident"
        assert any(v.field == "decision" for v in exc_info.value.violations)

    def test_unknown_severity_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_triage(_triage(severity="P5"))
        assert any(v.field == "severity" for v in exc_info.value.violations)

    def test_unknown_blast_radius_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_triage(_triage(blast_radius="galaxy_brain"))
        assert any(v.field == "blast_radius" for v in exc_info.value.violations)

    def test_negative_occurrences_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_triage(_triage(occurrences_24h=-1))
        assert any(v.field == "occurrences_24h" for v in exc_info.value.violations)

    def test_empty_reasoning_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_triage(_triage(reasoning=""))
        assert any(v.field == "reasoning" for v in exc_info.value.violations)

    def test_whitespace_only_reasoning_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_triage(_triage(reasoning="   "))
        assert any(v.field == "reasoning" for v in exc_info.value.violations)

    def test_multiple_violations_reported_together(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_triage(_triage(
                decision="bad", severity="X9", blast_radius="unknown", reasoning=""
            ))
        fields = {v.field for v in exc_info.value.violations}
        assert "decision" in fields
        assert "severity" in fields
        assert "reasoning" in fields


# ---------------------------------------------------------------------------
# validate_diagnosis — happy path
# ---------------------------------------------------------------------------

class TestValidateDiagnosisHappy:
    def test_valid_result_returned(self):
        r = _diagnosis()
        out = handoff_validator.validate_diagnosis(r)
        assert out.root_cause == "Missing S3 key"

    def test_confidence_zero_accepted(self):
        handoff_validator.validate_diagnosis(_diagnosis(confidence=0.0))

    def test_confidence_one_accepted(self):
        handoff_validator.validate_diagnosis(_diagnosis(confidence=1.0))

    def test_confidence_boundary_values(self):
        handoff_validator.validate_diagnosis(_diagnosis(confidence=0.0))
        handoff_validator.validate_diagnosis(_diagnosis(confidence=1.0))
        handoff_validator.validate_diagnosis(_diagnosis(confidence=0.5))


# ---------------------------------------------------------------------------
# validate_diagnosis — violations
# ---------------------------------------------------------------------------

class TestValidateDiagnosisViolations:
    def test_empty_root_cause_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_diagnosis(_diagnosis(root_cause=""))
        assert exc_info.value.stage == "diagnosis→incident"
        assert any(v.field == "root_cause" for v in exc_info.value.violations)

    def test_whitespace_root_cause_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_diagnosis(_diagnosis(root_cause="   "))
        assert any(v.field == "root_cause" for v in exc_info.value.violations)

    def test_confidence_above_1_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_diagnosis(_diagnosis(confidence=1.5))
        assert any(v.field == "confidence" for v in exc_info.value.violations)

    def test_confidence_below_0_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_diagnosis(_diagnosis(confidence=-0.1))
        assert any(v.field == "confidence" for v in exc_info.value.violations)

    def test_none_confidence_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_diagnosis(_diagnosis(confidence=None))
        assert any(v.field == "confidence" for v in exc_info.value.violations)

    def test_both_violations_reported(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_diagnosis(_diagnosis(root_cause="", confidence=2.0))
        fields = {v.field for v in exc_info.value.violations}
        assert "root_cause" in fields
        assert "confidence" in fields


# ---------------------------------------------------------------------------
# validate_fix_for_review — happy path
# ---------------------------------------------------------------------------

class TestValidateFixForReviewHappy:
    def test_valid_fix_does_not_raise(self):
        handoff_validator.validate_fix_for_review(_fix())

    def test_pr_number_1_accepted(self):
        handoff_validator.validate_fix_for_review(_fix(pr_number=1))


# ---------------------------------------------------------------------------
# validate_fix_for_review — violations
# ---------------------------------------------------------------------------

class TestValidateFixForReviewViolations:
    def test_none_pr_number_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_fix_for_review(_fix(pr_number=None))
        assert exc_info.value.stage == "fix→review"
        assert any(v.field == "pr_number" for v in exc_info.value.violations)

    def test_none_pr_url_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_fix_for_review(_fix(pr_url=None))
        assert any(v.field == "pr_url" for v in exc_info.value.violations)

    def test_empty_pr_url_raises(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_fix_for_review(_fix(pr_url=""))
        assert any(v.field == "pr_url" for v in exc_info.value.violations)

    def test_both_missing_reports_both(self):
        with pytest.raises(HandoffValidationError) as exc_info:
            handoff_validator.validate_fix_for_review(_fix(pr_number=None, pr_url=None))
        fields = {v.field for v in exc_info.value.violations}
        assert "pr_number" in fields
        assert "pr_url" in fields


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_handoff_validator_is_instance(self):
        assert isinstance(handoff_validator, HandoffValidator)
