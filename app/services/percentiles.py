"""
Shared percentile math for in-memory latency/metric trackers.

Extracted from latency.py so dedup_metrics.py (and anything else that needs
p25/p50/p75/p95 over a rolling sample window) doesn't reimplement it.
"""
from __future__ import annotations

import math
import statistics


def percentile(sorted_values: list[float], pct: float) -> float:
    """
    Return the pct-th percentile of a pre-sorted list (0 < pct <= 100).
    Uses nearest-rank method.
    """
    if not sorted_values:
        raise ValueError("empty list")
    idx = max(0, math.ceil(len(sorted_values) * pct / 100) - 1)
    return round(sorted_values[min(idx, len(sorted_values) - 1)], 1)


def stats(samples: list[float]) -> dict:
    """Return p25/p50/p75/p95/p99/min/max/mean for a list of durations (ms)."""
    if not samples:
        return {"count": 0, "p25": None, "p50": None, "p75": None, "p95": None,
                "p99": None, "min": None, "max": None, "mean": None}
    s = sorted(samples)
    return {
        "count": len(s),
        "p25":   percentile(s, 25),
        "p50":   percentile(s, 50),
        "p75":   percentile(s, 75),
        "p95":   percentile(s, 95),
        "p99":   percentile(s, 99),
        "min":   round(s[0], 1),
        "max":   round(s[-1], 1),
        "mean":  round(statistics.mean(s), 1),
    }
