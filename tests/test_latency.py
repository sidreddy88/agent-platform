"""
Tests for LatencyTracker — per-agent and pipeline-stage percentiles.

Run:
    pytest tests/test_latency.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from app.services.latency import LatencyTracker, _percentile, _stats

# ---------------------------------------------------------------------------
# _percentile helper
# ---------------------------------------------------------------------------

class TestPercentileHelper:
    def test_p50_of_sorted_values(self):
        # median of [1, 2, 3, 4, 5] = 3
        assert _percentile([1, 2, 3, 4, 5], 50) == 3

    def test_p95_of_100_values(self):
        values = list(range(1, 101))  # 1..100
        result = _percentile(values, 95)
        # nearest-rank: index = int(100 * 95/100) - 1 = 94 → value 95
        assert result == 95

    def test_p99_of_100_values(self):
        values = list(range(1, 101))
        result = _percentile(values, 99)
        assert result == 99

    def test_single_value(self):
        assert _percentile([42.0], 50) == 42.0
        assert _percentile([42.0], 99) == 42.0

    def test_raises_on_empty(self):
        with pytest.raises(ValueError):
            _percentile([], 50)


class TestStatsHelper:
    def test_empty_returns_none_values(self):
        result = _stats([])
        assert result["count"] == 0
        assert result["p50"] is None
        assert result["p95"] is None

    def test_single_sample(self):
        result = _stats([100.0])
        assert result["count"] == 1
        assert result["p50"] == 100.0
        assert result["min"] == 100.0
        assert result["max"] == 100.0

    def test_mean_is_computed(self):
        result = _stats([100.0, 200.0, 300.0])
        assert result["mean"] == 200.0

    def test_sorted_order_does_not_matter(self):
        result_unsorted = _stats([300.0, 100.0, 200.0])
        result_sorted   = _stats([100.0, 200.0, 300.0])
        assert result_unsorted["p50"] == result_sorted["p50"]


# ---------------------------------------------------------------------------
# LatencyTracker — per-agent recording and percentiles
# ---------------------------------------------------------------------------

class TestLatencyTrackerAgents:
    def test_record_and_retrieve(self):
        tracker = LatencyTracker()
        tracker.record("TriageAgent", 1200.0)
        tracker.record("TriageAgent", 1500.0)
        result = tracker.agent_percentiles("TriageAgent")
        assert result["agent"] == "TriageAgent"
        assert result["count"] == 2

    def test_unknown_agent_returns_zero_count(self):
        tracker = LatencyTracker()
        result = tracker.agent_percentiles("UnknownAgent")
        assert result["count"] == 0
        assert result["p50"] is None

    def test_multiple_agents_tracked_independently(self):
        tracker = LatencyTracker()
        for ms in [1000, 1100, 1200]:
            tracker.record("TriageAgent", ms)
        for ms in [3000, 3500, 4000]:
            tracker.record("DiagnosisAgent", ms)

        triage = tracker.agent_percentiles("TriageAgent")
        diagnosis = tracker.agent_percentiles("DiagnosisAgent")

        assert triage["count"] == 3
        assert diagnosis["count"] == 3
        assert triage["p50"] < diagnosis["p50"]

    def test_all_percentiles_returns_all_agents_sorted(self):
        tracker = LatencyTracker()
        tracker.record("ZAgent", 1000.0)
        tracker.record("AAgent", 2000.0)

        results = tracker.all_percentiles()
        assert len(results) == 2
        assert results[0]["agent"] == "AAgent"  # sorted alphabetically
        assert results[1]["agent"] == "ZAgent"

    def test_rolling_window_evicts_oldest(self):
        tracker = LatencyTracker(max_samples=3)
        for ms in [100, 200, 300, 400]:  # 4 samples in window of 3
            tracker.record("Agent", ms)

        result = tracker.agent_percentiles("Agent")
        assert result["count"] == 3
        assert result["min"] == 200.0  # 100 was evicted

    def test_p50_p95_p99_ordering(self):
        tracker = LatencyTracker()
        # 100 samples: uniform distribution 100ms..10000ms
        for i in range(100):
            tracker.record("Agent", float((i + 1) * 100))

        result = tracker.agent_percentiles("Agent")
        assert result["p50"] <= result["p95"] <= result["p99"]
        assert result["p99"] > result["p50"]

    def test_agent_names(self):
        tracker = LatencyTracker()
        tracker.record("TriageAgent", 1000.0)
        tracker.record("DiagnosisAgent", 2000.0)
        assert set(tracker.agent_names()) == {"TriageAgent", "DiagnosisAgent"}


# ---------------------------------------------------------------------------
# Pipeline stage percentiles (from IncidentStore timestamps)
# ---------------------------------------------------------------------------

class TestPipelineStagePercentiles:
    def _make_incident(
        self,
        detected_offset=0,
        triage_offset=None,
        diagnosis_offset=None,
        fix_offset=None,
        resolved_offset=None,
    ):
        """Build a minimal IncidentState with controlled timestamps."""
        from app.models.events import ErrorEvent, EventSource, IncidentState, IncidentStatus

        base = datetime(2026, 4, 12, 10, 0, 0)

        event = ErrorEvent(
            source=EventSource.CLOUDWATCH,
            error_type="S3_NO_SUCH_KEY",
            title="test",
            description="test",
            service="svc",
        )
        inc = IncidentState(error_event=event)
        inc.detected_at = base + timedelta(seconds=detected_offset)

        if triage_offset is not None:
            inc.triage_completed_at = base + timedelta(seconds=triage_offset)
        if diagnosis_offset is not None:
            inc.diagnosis_completed_at = base + timedelta(seconds=diagnosis_offset)
        if fix_offset is not None:
            inc.pr_created_at = base + timedelta(seconds=fix_offset)
        if resolved_offset is not None:
            inc.resolved_at = base + timedelta(seconds=resolved_offset)
            inc.status = IncidentStatus.RESOLVED

        return inc

    def test_triage_stage_computed_correctly(self):
        tracker = LatencyTracker()
        # Triage took 5 seconds → 5000ms
        inc = self._make_incident(detected_offset=0, triage_offset=5)

        with patch("app.services.latency.incident_store") as mock_store:
            mock_store.list_all.return_value = [inc]
            stages = tracker.pipeline_stage_percentiles()

        triage = next(s for s in stages if s["stage"] == "triage")
        assert triage["count"] == 1
        assert triage["p50"] == 5000.0

    def test_diagnosis_stage_uses_triage_as_start(self):
        tracker = LatencyTracker()
        # Triage done at +5s, diagnosis done at +15s → 10 seconds
        inc = self._make_incident(
            detected_offset=0, triage_offset=5, diagnosis_offset=15
        )

        with patch("app.services.latency.incident_store") as mock_store:
            mock_store.list_all.return_value = [inc]
            stages = tracker.pipeline_stage_percentiles()

        diagnosis = next(s for s in stages if s["stage"] == "diagnosis")
        assert diagnosis["p50"] == 10_000.0

    def test_mttr_is_end_to_end(self):
        tracker = LatencyTracker()
        # Resolved at +60 seconds → MTTR = 60s = 60000ms
        inc = self._make_incident(
            detected_offset=0,
            triage_offset=5,
            diagnosis_offset=15,
            fix_offset=40,
            resolved_offset=60,
        )

        with patch("app.services.latency.incident_store") as mock_store:
            mock_store.list_all.return_value = [inc]
            stages = tracker.pipeline_stage_percentiles()

        mttr = next(s for s in stages if s["stage"] == "mttr")
        assert mttr["count"] == 1
        assert mttr["p50"] == 60_000.0

    def test_missing_timestamps_not_included(self):
        tracker = LatencyTracker()
        # Only triage is complete — no diagnosis, fix, or resolved timestamps
        inc = self._make_incident(detected_offset=0, triage_offset=5)

        with patch("app.services.latency.incident_store") as mock_store:
            mock_store.list_all.return_value = [inc]
            stages = tracker.pipeline_stage_percentiles()

        triage = next(s for s in stages if s["stage"] == "triage")
        diagnosis = next(s for s in stages if s["stage"] == "diagnosis")
        fix = next(s for s in stages if s["stage"] == "fix")
        mttr = next(s for s in stages if s["stage"] == "mttr")

        assert triage["count"] == 1
        assert diagnosis["count"] == 0
        assert fix["count"] == 0
        assert mttr["count"] == 0

    def test_multiple_incidents_produce_percentiles(self):
        tracker = LatencyTracker()
        # 3 incidents with different triage durations: 2s, 5s, 8s
        incidents = [
            self._make_incident(detected_offset=0, triage_offset=2),
            self._make_incident(detected_offset=0, triage_offset=5),
            self._make_incident(detected_offset=0, triage_offset=8),
        ]

        with patch("app.services.latency.incident_store") as mock_store:
            mock_store.list_all.return_value = incidents
            stages = tracker.pipeline_stage_percentiles()

        triage = next(s for s in stages if s["stage"] == "triage")
        assert triage["count"] == 3
        assert triage["min"] == 2000.0
        assert triage["max"] == 8000.0

    def test_all_four_stages_always_returned(self):
        tracker = LatencyTracker()

        with patch("app.services.latency.incident_store") as mock_store:
            mock_store.list_all.return_value = []
            stages = tracker.pipeline_stage_percentiles()

        stage_names = {s["stage"] for s in stages}
        assert stage_names == {"triage", "diagnosis", "fix", "mttr"}


# ---------------------------------------------------------------------------
# summary() combines agents + stages
# ---------------------------------------------------------------------------

class TestSummary:
    def test_summary_contains_both_keys(self):
        tracker = LatencyTracker()
        tracker.record("TriageAgent", 1000.0)

        with patch("app.services.latency.incident_store") as mock_store:
            mock_store.list_all.return_value = []
            result = tracker.summary()

        assert "agents" in result
        assert "pipeline_stages" in result

    def test_summary_agents_match_all_percentiles(self):
        tracker = LatencyTracker()
        tracker.record("TriageAgent", 1000.0)
        tracker.record("DiagnosisAgent", 3000.0)

        with patch("app.services.latency.incident_store") as mock_store:
            mock_store.list_all.return_value = []
            result = tracker.summary()

        assert len(result["agents"]) == 2


# ---------------------------------------------------------------------------
# trace_agent integration — latency auto-recorded on every agent run
# ---------------------------------------------------------------------------

class TestTraceAgentIntegration:
    @pytest.mark.asyncio
    async def test_agent_run_records_latency(self):
        """trace_agent decorator records duration_ms in latency_tracker."""
        from app.agents.base import AgentResult, BaseAgent
        from app.services.latency import latency_tracker

        class _DummyAgent(BaseAgent):
            async def run(self, user_input: str) -> AgentResult:
                return AgentResult(answer="done", steps=[], iterations=1)

        agent = _DummyAgent.__new__(_DummyAgent)
        agent._tracing_ctx = None

        before_count = latency_tracker.agent_percentiles("_DummyAgent")["count"]

        # Call run() — trace_agent is applied in BaseAgent
        # We need to go through the actual decorated method
        from app.services.tracing import trace_agent

        @trace_agent
        async def _run(self, user_input):
            return AgentResult(answer="done", steps=[], iterations=1)

        with patch("app.services.latency.latency_tracker", latency_tracker):
            await _run(agent, "test input")

        after_count = latency_tracker.agent_percentiles("_DummyAgent")["count"]
        assert after_count == before_count + 1
