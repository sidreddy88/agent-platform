"""
TriageMetrics — production health tracking for TriageAgent.

TriageAgent itself already has latency (latency_tracker, via @trace_agent)
and cost (llm_gateway.costs_today()) tracked automatically — this module
only adds the two things that don't exist anywhere yet:

  1. Silent-fallback rate — incident_loop._run_triage() catches both
     HandoffValidationError and any generic Exception and defaults to
     decision="real"/severity="P2" with zero aggregate visibility (only a
     per-incident logger.error line). Same shape of blind spot as the dedup
     gate's swallowed RAG errors — this is the one genuinely new counter.

  2. Decision + severity distribution drift — a regression here doesn't
     crash, it just silently reshapes the real/noise/duplicate or P0-P3
     mix. Computed from persisted IncidentState (triage_decision, severity,
     triage_completed_at) via incident_store.list_all(), the same
     ground-truth-over-live-store pattern dedup_metrics.duplicate_leaks()
     already uses — no new writes needed, this data already exists.

The CI regression gate (triage-regression.yml) validates against a frozen
80-case dataset on every PR — it answers "did this change regress vs
history?", not "is triage behaving normally on live traffic right now?".
This module is for the second question.

Usage:
    from app.services.triage_metrics import triage_metrics
    triage_metrics.record_fallback("schema_invalid")   # or "exception"
    triage_metrics.summary()
"""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timezone

FALLBACK_REASONS = ["schema_invalid", "exception"]
DECISIONS = ["real", "noise", "duplicate"]
SEVERITIES = ["P0", "P1", "P2", "P3"]


def _hour_bucket(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:00")


class TriageMetrics:
    """Rolling in-memory fallback tracker + persisted-store distribution queries."""

    def __init__(self, max_hours: int = 24 * 7) -> None:
        self._max_hours = max_hours
        self._fallback_hourly: dict[str, dict[str, int]] = defaultdict(
            lambda: {k: 0 for k in FALLBACK_REASONS}
        )
        self._total_hourly: dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record_triage(self) -> None:
        """Call once per triage attempt (success or fallback) — the denominator
        for fallback rate."""
        self._total_hourly[_hour_bucket(datetime.now(timezone.utc))] += 1

    def record_fallback(self, reason: str) -> None:
        """reason: 'schema_invalid' (HandoffValidationError) or 'exception'
        (anything else) — the two except branches in incident_loop._run_triage."""
        bucket = _hour_bucket(datetime.now(timezone.utc))
        self._fallback_hourly[bucket][reason] = self._fallback_hourly[bucket].get(reason, 0) + 1
        self._prune()

    def _prune(self) -> None:
        for store in (self._fallback_hourly, self._total_hourly):
            if len(store) > self._max_hours:
                oldest = sorted(store.keys())[: len(store) - self._max_hours]
                for k in oldest:
                    del store[k]

    # ------------------------------------------------------------------
    # Fallback reading
    # ------------------------------------------------------------------

    def fallback_summary(self, window_hours: int = 24) -> dict:
        buckets = sorted(set(self._fallback_hourly.keys()) | set(self._total_hourly.keys()))[-window_hours:]
        totals: dict[str, int] = defaultdict(int)
        total_triages = 0
        for b in buckets:
            for reason in FALLBACK_REASONS:
                totals[reason] += self._fallback_hourly.get(b, {}).get(reason, 0)
            total_triages += self._total_hourly.get(b, 0)
        total_fallbacks = sum(totals[r] for r in FALLBACK_REASONS)
        return {
            "window_hours": window_hours,
            "total_triages": total_triages,
            "fallback_counts": dict(totals),
            "total_fallbacks": total_fallbacks,
            "fallback_rate": round(total_fallbacks / total_triages, 4) if total_triages else None,
        }

    # ------------------------------------------------------------------
    # Distribution drift (ground truth — persisted IncidentState)
    # ------------------------------------------------------------------

    def decision_distribution(self, hours: int = 48) -> list[dict]:
        """Hourly real/noise/duplicate counts, bucketed on triage_completed_at."""
        from app.services.incident_store import incident_store

        buckets: dict[str, dict[str, int]] = defaultdict(lambda: {k: 0 for k in DECISIONS})
        for inc in incident_store.list_all():
            if not inc.triage_completed_at or not inc.triage_decision:
                continue
            if inc.triage_decision not in DECISIONS:
                continue
            b = _hour_bucket(inc.triage_completed_at)
            buckets[b][inc.triage_decision] += 1

        ordered = sorted(buckets.keys())[-hours:]
        return [{"hour": h, **buckets[h]} for h in ordered]

    def severity_distribution(self, hours: int = 48) -> list[dict]:
        """Hourly P0-P3 counts, bucketed on triage_completed_at. Only counts
        decision="real" incidents — severity is meaningless for noise/duplicate."""
        from app.services.incident_store import incident_store

        buckets: dict[str, dict[str, int]] = defaultdict(lambda: {k: 0 for k in SEVERITIES})
        for inc in incident_store.list_all():
            severity = inc.error_event.severity
            if not inc.triage_completed_at or inc.triage_decision != "real" or not severity:
                continue
            sev = severity.value if hasattr(severity, "value") else str(severity)
            if sev not in SEVERITIES:
                continue
            b = _hour_bucket(inc.triage_completed_at)
            buckets[b][sev] += 1

        ordered = sorted(buckets.keys())[-hours:]
        return [{"hour": h, **buckets[h]} for h in ordered]

    # ------------------------------------------------------------------
    # Combined snapshot
    # ------------------------------------------------------------------

    def summary(self, window_hours: int = 24) -> dict:
        decisions = self.decision_distribution(window_hours)
        severities = self.severity_distribution(window_hours)

        decision_totals: dict[str, int] = defaultdict(int)
        for b in decisions:
            for k in DECISIONS:
                decision_totals[k] += b.get(k, 0)

        severity_totals: dict[str, int] = defaultdict(int)
        for b in severities:
            for k in SEVERITIES:
                severity_totals[k] += b.get(k, 0)

        return {
            "window_hours": window_hours,
            "fallback": self.fallback_summary(window_hours),
            "decision_totals": dict(decision_totals),
            "severity_totals": dict(severity_totals),
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

triage_metrics = TriageMetrics()
