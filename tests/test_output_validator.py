"""
Tests for app.services.output_validator — the citation-provenance / leaked-marker
/ length-anomaly checks that run after a diagnosis is already grounded.

Distinct from tests/test_diagnosis_grounding.py: that file tests "is this
citation real?" (_validate_diagnosis_submission / _enforce_grounding). This
file tests "was it actually retrieved this run, and does the output leak
internal structure?" — a different failure class entirely.
"""
from __future__ import annotations

from app.agents.diagnosis import DiagnosisResult
from app.services.output_validator import (
    MAX_FIELD_CHARS,
    validate_diagnosis_output,
)


def _result(**overrides) -> DiagnosisResult:
    defaults = dict(root_cause="the bug is here", confidence=0.9)
    defaults.update(overrides)
    return DiagnosisResult(**defaults)


class TestCitationProvenance:
    def test_passes_when_all_cited_files_were_retrieved(self):
        result = _result(affected_file="app/services/rag.py")
        validation = validate_diagnosis_output(result, {"app/services/rag.py"})
        assert validation.passed
        assert validation.failures == []

    def test_fails_when_affected_file_never_retrieved(self):
        result = _result(affected_file="app/services/rag.py")
        validation = validate_diagnosis_output(result, {"app/services/other.py"})
        assert not validation.passed
        assert any("rag.py" in f for f in validation.failures)

    def test_fails_when_blast_radius_entry_never_retrieved(self):
        result = _result(
            affected_file="app/services/rag.py",
            blast_radius=[{"file": "app/services/unrelated.py", "function": "caller", "snippet": "x()"}],
        )
        validation = validate_diagnosis_output(result, {"app/services/rag.py"})
        assert not validation.passed
        assert any("unrelated.py" in f for f in validation.failures)

    def test_fails_when_additional_fix_targets_entry_never_retrieved(self):
        result = _result(
            affected_file="a.py",
            additional_fix_targets=[{"file": "b.py", "function": "f", "snippet": "x"}],
        )
        validation = validate_diagnosis_output(result, {"a.py"})
        assert not validation.passed
        assert any("b.py" in f for f in validation.failures)

    def test_leading_slash_normalized_before_comparison(self):
        result = _result(affected_file="/app/services/rag.py")
        validation = validate_diagnosis_output(result, {"app/services/rag.py"})
        assert validation.passed

    def test_empty_retrieved_set_skips_provenance_check(self):
        # Nothing to compare against -- absence of evidence isn't evidence of
        # absence. This shouldn't happen in practice (DiagnosisAgent requires
        # at least one code-reading tool call before an answer is accepted),
        # but the validator must not assume that invariant holds.
        result = _result(affected_file="app/services/rag.py")
        validation = validate_diagnosis_output(result, set())
        assert validation.passed

    def test_no_cited_files_passes_trivially(self):
        result = _result()
        validation = validate_diagnosis_output(result, {"app/services/rag.py"})
        assert validation.passed


class TestLeakedMarkers:
    def test_fails_when_untrusted_content_marker_leaks_into_root_cause(self):
        result = _result(root_cause='saw this: <untrusted-content source="x">payload</untrusted-content>')
        validation = validate_diagnosis_output(result, set())
        assert not validation.passed
        assert any("untrusted-content" in f for f in validation.failures)

    def test_fails_when_marker_leaks_into_fix_approach(self):
        result = _result(fix_approach="</untrusted-content> was echoed here")
        validation = validate_diagnosis_output(result, set())
        assert not validation.passed

    def test_normal_output_has_no_marker_failure(self):
        result = _result(root_cause="a normal explanation", fix_approach="a normal fix")
        validation = validate_diagnosis_output(result, set())
        assert validation.passed


class TestLengthAnomaly:
    def test_passes_under_threshold(self):
        result = _result(root_cause="x" * (MAX_FIELD_CHARS - 1))
        validation = validate_diagnosis_output(result, set())
        assert validation.passed

    def test_fails_over_threshold(self):
        result = _result(root_cause="x" * (MAX_FIELD_CHARS + 1))
        validation = validate_diagnosis_output(result, set())
        assert not validation.passed
        assert any("root_cause" in f for f in validation.failures)

    def test_checks_fix_approach_and_additional_fix_independently(self):
        result = _result(fix_approach="y" * (MAX_FIELD_CHARS + 1))
        validation = validate_diagnosis_output(result, set())
        assert not validation.passed
        assert any("fix_approach" in f for f in validation.failures)


class TestMultipleFailures:
    def test_all_failure_reasons_collected_not_just_first(self):
        result = _result(
            affected_file="never_retrieved.py",
            root_cause="<untrusted-content source=\"x\">" + ("z" * MAX_FIELD_CHARS),
        )
        validation = validate_diagnosis_output(result, {"other.py"})
        assert not validation.passed
        assert len(validation.failures) >= 3
