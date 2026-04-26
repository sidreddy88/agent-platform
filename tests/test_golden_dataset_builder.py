"""
Tests for GoldenDatasetBuilder — auto-capture of real incident traces.

Run:
    pytest tests/test_golden_dataset_builder.py -v
"""
from __future__ import annotations

import json
from datetime import datetime

from app.services.golden_dataset_builder import GoldenDatasetBuilder, golden_dataset_builder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_incident(
    *,
    error_type: str = "S3_NO_SUCH_KEY",
    service: str = "image-service",
    triage_decision: str = "real",
    triage_reasoning: str = "Recurring error in production",
    confidence: float | None = 0.85,
    human_decision: str | None = None,
    human_decision_reason: str | None = None,
    outcome: str | None = None,
    pr_url: str | None = None,
    pr_number: int | None = None,
):
    from app.models.events import ErrorEvent, EventSource, IncidentState, Severity
    event = ErrorEvent(
        source=EventSource.APPLICATION,
        error_type=error_type,
        title=f"{error_type} in {service}",
        description="Test incident",
        service=service,
        severity=Severity.P2,
    )
    inc = IncidentState(error_event=event)
    inc.triage_decision = triage_decision
    inc.triage_reasoning = triage_reasoning
    inc.confidence = confidence
    inc.human_decision = human_decision
    inc.human_decision_reason = human_decision_reason
    inc.outcome = outcome
    inc.pr_url = pr_url
    inc.pr_number = pr_number
    return inc


# ---------------------------------------------------------------------------
# capture — quality filter
# ---------------------------------------------------------------------------

class TestCapture:
    def test_captures_resolved_incident(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc = _make_incident(human_decision="approved", outcome="fix_merged")
        record = gdb.capture(inc)
        assert record is not None
        assert record["id"].startswith("auto_")

    def test_captures_rejected_incident(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc = _make_incident(confidence=0.45, human_decision="rejected", outcome="fix_rejected")
        record = gdb.capture(inc)
        assert record is not None

    def test_captures_duplicate_decision(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc = _make_incident(triage_decision="duplicate", confidence=None)
        record = gdb.capture(inc)
        assert record is not None

    def test_captures_high_confidence_auto(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc = _make_incident(confidence=0.90)
        record = gdb.capture(inc)
        assert record is not None

    def test_skips_low_confidence_no_human_decision(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc = _make_incident(confidence=0.45, human_decision=None)
        record = gdb.capture(inc)
        assert record is None
        assert gdb.captured_count == 0

    def test_skips_exactly_at_threshold_minus_epsilon(self, tmp_path):
        """confidence = 0.599 (just below 0.60) without human decision → skip."""
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc = _make_incident(confidence=0.599, human_decision=None)
        assert gdb.capture(inc) is None

    def test_captures_at_threshold(self, tmp_path):
        """confidence = 0.60 (exactly at threshold) → capture."""
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc = _make_incident(confidence=0.60, human_decision=None)
        assert gdb.capture(inc) is not None

    def test_always_captures_human_adjudicated_low_confidence(self, tmp_path):
        """Human decision overrides confidence threshold."""
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc = _make_incident(confidence=0.10, human_decision="rejected")
        record = gdb.capture(inc)
        assert record is not None

    def test_counter_increments(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        assert gdb.captured_count == 0
        gdb.capture(_make_incident(human_decision="approved"))
        assert gdb.captured_count == 1
        gdb.capture(_make_incident(error_type="DB_CONN_FAIL", human_decision="approved"))
        assert gdb.captured_count == 2


# ---------------------------------------------------------------------------
# capture — noise decisions
# ---------------------------------------------------------------------------

class TestCaptureNoise:
    def test_captures_noise_with_new_error_type(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc = _make_incident(triage_decision="noise", confidence=None, error_type="HEALTH_CHECK_FLAP")
        record = gdb.capture(inc)
        assert record is not None

    def test_skips_noise_with_existing_error_type(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        # First noise of this type → captured
        inc1 = _make_incident(triage_decision="noise", confidence=None, error_type="HEALTH_CHECK_FLAP")
        gdb.capture(inc1)
        # Second noise of the same type → skipped
        inc2 = _make_incident(triage_decision="noise", confidence=None, error_type="HEALTH_CHECK_FLAP")
        result = gdb.capture(inc2)
        assert result is None
        assert gdb.captured_count == 1


# ---------------------------------------------------------------------------
# Deduplication by error_type
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_skips_duplicate_error_type(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc1 = _make_incident(error_type="S3_NO_SUCH_KEY", confidence=0.90)
        inc2 = _make_incident(error_type="S3_NO_SUCH_KEY", confidence=0.95)

        gdb.capture(inc1)
        result = gdb.capture(inc2)

        assert result is None
        assert gdb.captured_count == 1

    def test_captures_different_error_types(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        gdb.capture(_make_incident(error_type="S3_NO_SUCH_KEY", confidence=0.90))
        gdb.capture(_make_incident(error_type="DB_CONNECTION_POOL_EXHAUSTED", confidence=0.88))
        assert gdb.captured_count == 2

    def test_human_adjudicated_always_captured_even_if_dup_type(self, tmp_path):
        """Human decisions are always kept regardless of error_type dedup."""
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        gdb.capture(_make_incident(error_type="S3_NO_SUCH_KEY", confidence=0.90))
        # Same error_type but human-adjudicated → always capture
        inc2 = _make_incident(error_type="S3_NO_SUCH_KEY", human_decision="rejected")
        result = gdb.capture(inc2)
        assert result is not None
        assert gdb.captured_count == 2


# ---------------------------------------------------------------------------
# Record format
# ---------------------------------------------------------------------------

class TestRecordFormat:
    def test_id_starts_with_auto(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        rec = gdb.capture(_make_incident(confidence=0.90))
        assert rec["id"].startswith("auto_")

    def test_standard_fields_present(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        rec = gdb.capture(_make_incident(confidence=0.90))
        for key in ("id", "description", "input", "expected", "tags"):
            assert key in rec, f"Missing field: {key}"

    def test_full_trace_present(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        inc = _make_incident(confidence=0.90, pr_url="https://github.com/org/repo/pull/1", pr_number=1)
        rec = gdb.capture(inc)
        ft = rec["full_trace"]
        assert "confidence" in ft
        assert "diagnosis" in ft
        assert ft["pr_url"] == "https://github.com/org/repo/pull/1"

    def test_input_fields(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        rec = gdb.capture(_make_incident(error_type="S3_NO_SUCH_KEY", confidence=0.90))
        assert rec["input"]["error_type"] == "S3_NO_SUCH_KEY"
        assert rec["input"]["service"] == "image-service"

    def test_expected_fields(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        rec = gdb.capture(_make_incident(triage_decision="real", confidence=0.90))
        assert rec["expected"]["triage_decision"] == "real"
        assert isinstance(rec["expected"]["triage_severity"], list)

    def test_captured_at_is_iso_format(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        rec = gdb.capture(_make_incident(confidence=0.90))
        datetime.fromisoformat(rec["captured_at"])  # must not raise

    def test_tags_include_auto_captured(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        rec = gdb.capture(_make_incident(confidence=0.90))
        assert "auto-captured" in rec["tags"]


# ---------------------------------------------------------------------------
# JSONL persistence
# ---------------------------------------------------------------------------

class TestJSONLPersistence:
    def test_file_created_on_first_capture(self, tmp_path):
        path = tmp_path / "dataset.jsonl"
        gdb = GoldenDatasetBuilder(path=path)
        assert not path.exists()
        gdb.capture(_make_incident(confidence=0.90))
        assert path.exists()

    def test_each_line_is_valid_json(self, tmp_path):
        path = tmp_path / "dataset.jsonl"
        gdb = GoldenDatasetBuilder(path=path)
        gdb.capture(_make_incident(error_type="ERR_A", confidence=0.90))
        gdb.capture(_make_incident(error_type="ERR_B", confidence=0.90))
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert "id" in obj

    def test_multiple_captures_append_not_overwrite(self, tmp_path):
        path = tmp_path / "dataset.jsonl"
        gdb = GoldenDatasetBuilder(path=path)
        error_types = [f"ERR_{i}" for i in range(5)]
        for et in error_types:
            gdb.capture(_make_incident(error_type=et, confidence=0.90))
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 5

    def test_records_survive_new_instance(self, tmp_path):
        path = tmp_path / "dataset.jsonl"
        gdb1 = GoldenDatasetBuilder(path=path)
        gdb1.capture(_make_incident(confidence=0.90))

        gdb2 = GoldenDatasetBuilder(path=path)
        records = gdb2.get_all()
        assert len(records) == 1
        assert records[0]["id"].startswith("auto_")


# ---------------------------------------------------------------------------
# get_all / count
# ---------------------------------------------------------------------------

class TestGetAllAndCount:
    def test_empty_when_no_file(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        assert gdb.get_all() == []

    def test_returns_all_records(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        gdb.capture(_make_incident(error_type="ERR_A", confidence=0.90))
        gdb.capture(_make_incident(error_type="ERR_B", confidence=0.90))
        assert len(gdb.get_all()) == 2

    def test_skips_malformed_lines(self, tmp_path):
        path = tmp_path / "dataset.jsonl"
        path.write_text('{"id":"auto_good1"}\nnot-json\n{"id":"auto_good2"}\n')
        gdb = GoldenDatasetBuilder(path=path)
        records = gdb.get_all()
        assert len(records) == 2

    def test_count_only_auto_records(self, tmp_path):
        """count() excludes hand-crafted records (id starts with 'eval_')."""
        path = tmp_path / "dataset.jsonl"
        # Simulate hand-crafted + auto-captured in same file
        path.write_text(
            '{"id":"eval_001","description":"hand-crafted"}\n'
            '{"id":"auto_abc12345","description":"auto-captured"}\n'
        )
        gdb = GoldenDatasetBuilder(path=path)
        assert gdb.count() == 1

    def test_count_all_auto(self, tmp_path):
        gdb = GoldenDatasetBuilder(path=tmp_path / "dataset.jsonl")
        gdb.capture(_make_incident(error_type="ERR_A", confidence=0.90))
        gdb.capture(_make_incident(error_type="ERR_B", confidence=0.90))
        assert gdb.count() == 2


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_is_instance(self):
        assert isinstance(golden_dataset_builder, GoldenDatasetBuilder)

    def test_singleton_path_is_real_dataset(self):
        from app.services.golden_dataset_builder import _DEFAULT_PATH
        assert _DEFAULT_PATH.name == "golden_dataset.jsonl"
        assert "evals" in str(_DEFAULT_PATH)
