"""
LatencyTracker — per-agent and per-pipeline-stage latency percentiles.

Collects duration_ms samples in a rolling in-memory window and computes
p50 / p95 / p99 on demand.  Also derives pipeline-stage percentiles from
the timestamps already stored in IncidentStore (no extra instrumentation
needed for stage latency).

Two data sources:
  1. Agent latency  — recorded by trace_agent decorator on every agent run
  2. Pipeline stage — derived from incident.triage_completed_at,
                      diagnosis_completed_at, pr_created_at, resolved_at

Usage:
    # Recorded automatically via trace_agent — no manual calls needed.
    # Query:
    from app.services.latency import latency_tracker
    print(latency_tracker.all_percentiles())
    print(latency_tracker.pipeline_stage_percentiles())
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict, deque
from typing import Optional

from app.services.incident_store import incident_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(sorted_values: list[float], pct: float) -> float:
    """
    Return the pct-th percentile of a pre-sorted list (0 < pct <= 100).
    Uses nearest-rank method.
    """
    if not sorted_values:
        raise ValueError("empty list")
    idx = max(0, math.ceil(len(sorted_values) * pct / 100) - 1)
    return round(sorted_values[min(idx, len(sorted_values) - 1)], 1)


def _stats(samples: list[float]) -> dict:
    """Return p50/p95/p99/min/max/mean for a list of durations (ms)."""
    if not samples:
        return {"count": 0, "p50": None, "p95": None, "p99": None,
                "min": None, "max": None, "mean": None}
    s = sorted(samples)
    return {
        "count": len(s),
        "p50":   _percentile(s, 50),
        "p95":   _percentile(s, 95),
        "p99":   _percentile(s, 99),
        "min":   round(s[0], 1),
        "max":   round(s[-1], 1),
        "mean":  round(statistics.mean(s), 1),
    }


# ---------------------------------------------------------------------------
# LatencyTracker
# ---------------------------------------------------------------------------

class LatencyTracker:
    """
    Rolling in-memory per-agent latency store.

    Thread-safe enough for asyncio (single-threaded event loop).
    Replace with a time-series DB (InfluxDB, Prometheus) for production.
    """

    def __init__(self, max_samples: int = 500) -> None:
        self._max = max_samples
        # {agent_name: deque[duration_ms]}
        self._agent_samples: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=self._max)
        )

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(self, agent_name: str, duration_ms: float) -> None:
        """Record one agent run duration.  Called by trace_agent decorator."""
        self._agent_samples[agent_name].append(duration_ms)

    # ------------------------------------------------------------------
    # Per-agent percentiles
    # ------------------------------------------------------------------

    def agent_percentiles(self, agent_name: str) -> dict:
        """p50/p95/p99 for a single agent."""
        samples = list(self._agent_samples.get(agent_name, []))
        return {"agent": agent_name, **_stats(samples)}

    def all_percentiles(self) -> list[dict]:
        """p50/p95/p99 for every tracked agent, sorted by name."""
        return [
            {"agent": name, **_stats(list(samples))}
            for name, samples in sorted(self._agent_samples.items())
        ]

    # ------------------------------------------------------------------
    # Pipeline stage percentiles (derived from IncidentStore timestamps)
    # ------------------------------------------------------------------

    def pipeline_stage_percentiles(self) -> list[dict]:
        """
        Compute p50/p95/p99 for each pipeline stage using the timestamps
        already stored on IncidentState objects — no extra instrumentation.

        Stages:
          triage       = triage_completed_at   − detected_at
          diagnosis    = diagnosis_completed_at − triage_completed_at
          fix          = pr_created_at          − diagnosis_completed_at
          mttr         = resolved_at            − detected_at  (end-to-end)
        """
        triage_ms: list[float] = []
        diagnosis_ms: list[float] = []
        fix_ms: list[float] = []
        mttr_ms: list[float] = []

        for inc in incident_store.list_all():
            if inc.triage_completed_at and inc.detected_at:
                triage_ms.append(
                    (inc.triage_completed_at - inc.detected_at).total_seconds() * 1000
                )
            if inc.diagnosis_completed_at and inc.triage_completed_at:
                diagnosis_ms.append(
                    (inc.diagnosis_completed_at - inc.triage_completed_at).total_seconds() * 1000
                )
            if inc.pr_created_at and inc.diagnosis_completed_at:
                fix_ms.append(
                    (inc.pr_created_at - inc.diagnosis_completed_at).total_seconds() * 1000
                )
            if inc.mttr_seconds is not None:
                mttr_ms.append(inc.mttr_seconds * 1000)

        return [
            {"stage": "triage",    **_stats(triage_ms)},
            {"stage": "diagnosis", **_stats(diagnosis_ms)},
            {"stage": "fix",       **_stats(fix_ms)},
            {"stage": "mttr",      **_stats(mttr_ms)},
        ]

    # ------------------------------------------------------------------
    # Summary (agents + stages combined)
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        return {
            "agents": self.all_percentiles(),
            "pipeline_stages": self.pipeline_stage_percentiles(),
        }

    def agent_names(self) -> list[str]:
        return sorted(self._agent_samples.keys())


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

latency_tracker = LatencyTracker()
