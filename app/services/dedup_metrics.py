"""
DedupMetrics — time-bucketed dedup-gate health tracking + per-layer latency.

Same pattern as LatencyTracker (rolling in-memory window; swap for a real
time-series DB in production) but tracks dedup *outcomes* per hour instead of
agent latency, plus per-layer latency percentiles and the one ground-truth
signal that actually proves the gate is working: whether a duplicate incident
leaked through anyway.

Five layer outcomes, in the fixed order they're evaluated in incident_loop.py:
  sql_dedup       — Layer 1 hard block (Postgres, normalized description match)
  regression      — Layer 2 soft context (Postgres, resolved-incident lookup)
  rag_hard_block  — Layer 3 hard block (RAG semantic search + live store lookup)
  rag_hit         — Layer 3b soft hint (RAG semantic search, below hard-block bar)
  cold_start       — no dedup signal at all; genuinely new error

rag_error is tracked separately, not as a sixth "outcome" on equal footing —
it's a failure signal (the RAG call itself broke), not a dedup decision.

Usage:
    from app.services.dedup_metrics import dedup_metrics
    dedup_metrics.record_outcome("rag_hard_block")
    dedup_metrics.record_latency("layer3_rag_search", 42.3)
    dedup_metrics.summary()
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone

from app.services.percentiles import stats as _stats

OUTCOMES = ["sql_dedup", "regression", "rag_hard_block", "rag_hit", "cold_start"]


def _hour_bucket(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:00")


class DedupMetrics:
    """Rolling in-memory dedup-gate health tracker."""

    def __init__(self, max_hours: int = 24 * 7, max_latency_samples: int = 500) -> None:
        self._max_hours = max_hours
        self._hourly: dict[str, dict[str, int]] = defaultdict(lambda: {k: 0 for k in OUTCOMES})
        self._rag_errors: dict[str, int] = defaultdict(int)
        self._latency: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=max_latency_samples)
        )

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_outcome(self, outcome: str) -> None:
        """Record one dedup-gate decision for the current hour bucket."""
        bucket = _hour_bucket(datetime.now(timezone.utc))
        if outcome == "rag_error":
            self._rag_errors[bucket] += 1
        else:
            self._hourly[bucket][outcome] = self._hourly[bucket].get(outcome, 0) + 1
        self._prune()

    def record_latency(self, stage: str, duration_ms: float) -> None:
        """Record one timed gate operation. Stages: layer1_sql, layer2_sql,
        layer3_rag_search, layer3_live_lookup."""
        self._latency[stage].append(duration_ms)

    def _prune(self) -> None:
        if len(self._hourly) > self._max_hours:
            oldest = sorted(self._hourly.keys())[: len(self._hourly) - self._max_hours]
            for k in oldest:
                del self._hourly[k]
        if len(self._rag_errors) > self._max_hours:
            oldest = sorted(self._rag_errors.keys())[: len(self._rag_errors) - self._max_hours]
            for k in oldest:
                del self._rag_errors[k]

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def timeseries(self, hours: int = 48) -> list[dict]:
        """Hourly outcome counts, oldest first, for the last `hours` buckets."""
        all_buckets = sorted(set(self._hourly.keys()) | set(self._rag_errors.keys()))
        buckets = all_buckets[-hours:]
        return [
            {"hour": b, **{k: self._hourly.get(b, {}).get(k, 0) for k in OUTCOMES},
             "rag_error": self._rag_errors.get(b, 0)}
            for b in buckets
        ]

    def latency_percentiles(self) -> list[dict]:
        """p25/p50/p75/p95 per gate stage."""
        return [
            {"stage": stage, **_stats(list(samples))}
            for stage, samples in sorted(self._latency.items())
        ]

    def duplicate_leaks(self, window_minutes: int = 60) -> list[dict]:
        """
        Ground-truth check: any two distinct incidents sharing error_type +
        service, detected within window_minutes of each other. If the dedup
        gate is healthy this is always empty — it's what the gate exists to
        prevent, checked independently of its own internal counters.
        """
        from app.services.incident_store import incident_store

        by_key: dict[tuple[str, str], list] = defaultdict(list)
        for inc in incident_store.list_all():
            error_type = inc.error_event.error_type
            service = inc.error_event.service
            if not error_type or not service:
                continue
            by_key[(error_type, service)].append(inc)

        leaks: list[dict] = []
        for (error_type, service), group in by_key.items():
            group.sort(key=lambda i: i.detected_at)
            for a, b in zip(group, group[1:]):
                delta_min = (b.detected_at - a.detected_at).total_seconds() / 60
                if 0 <= delta_min <= window_minutes:
                    leaks.append({
                        "error_type": error_type,
                        "service": service,
                        "incident_a": a.id,
                        "incident_b": b.id,
                        "minutes_apart": round(delta_min, 1),
                    })
        return leaks

    def summary(self, window_hours: int = 24) -> dict:
        """Rolled-up health snapshot for the dashboard's headline numbers."""
        window = self.timeseries(window_hours)
        totals: dict[str, int] = defaultdict(int)
        for bucket in window:
            for k in OUTCOMES:
                totals[k] += bucket.get(k, 0)
            totals["rag_error"] += bucket.get("rag_error", 0)

        total_events = sum(totals[k] for k in OUTCOMES)
        blocked = totals["sql_dedup"] + totals["rag_hard_block"]
        leaks = self.duplicate_leaks()

        return {
            "window_hours": window_hours,
            "outcomes": {k: totals[k] for k in OUTCOMES},
            "total_events": total_events,
            "block_rate": round(blocked / total_events, 3) if total_events else None,
            "rag_error_count": totals["rag_error"],
            "rag_error_rate": round(totals["rag_error"] / total_events, 3) if total_events else None,
            "duplicate_leak_count": len(leaks),
            "duplicate_leaks": leaks[:20],
            "healthy": totals["rag_error"] == 0 and len(leaks) == 0,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

dedup_metrics = DedupMetrics()
