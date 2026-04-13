"""
MonitorStore — in-memory store for recently generated monitor configs.

Holds the last 100 MonitorGenerationResult objects (one per PR merge).
Shared between the webhook handler (writer) and the monitors API (reader).

Usage:
    from app.services.monitor_store import monitor_store
    monitor_store.save(repo, pr_number, result)
    records = monitor_store.get_all()
    metrics = monitor_store.coverage_metrics()
"""
from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.agents.monitor_generation import MonitorGenerationResult


class MonitorStore:
    """In-memory deque of monitor generation results, newest last."""

    def __init__(self, maxlen: int = 100) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=maxlen)

    def save(self, repo: str, pr_number: int, result: "MonitorGenerationResult") -> None:
        """Store a MonitorGenerationResult."""
        self._records.append({
            "repo": repo,
            "pr_number": pr_number,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "files_analyzed": result.files_analyzed,
            "monitors_created": result.monitors_created,
            "coverage_ratio": result.coverage_ratio,
            "dry_run": result.dry_run,
            "monitors": [
                {
                    "monitor_type": m.monitor_type,
                    "file": m.file,
                    "name": m.name,
                    "config": m.config,
                    "created": m.created,
                }
                for m in result.monitors
            ],
        })

    def get_all(self) -> list[dict[str, Any]]:
        """Return all stored records, newest first."""
        return list(reversed(self._records))

    def coverage_metrics(self) -> dict[str, Any]:
        """Aggregate coverage metrics across all stored results."""
        records = list(self._records)
        if not records:
            return {
                "total_prs_covered": 0,
                "total_monitors_generated": 0,
                "avg_coverage_ratio": 0.0,
                "monitors_per_75_lines": None,
            }
        total_monitors = sum(r["monitors_created"] for r in records)
        ratios = [r["coverage_ratio"] for r in records if r["coverage_ratio"] > 0]
        avg_ratio = sum(ratios) / len(ratios) if ratios else 0.0
        return {
            "total_prs_covered": len(records),
            "total_monitors_generated": total_monitors,
            "avg_coverage_ratio": round(avg_ratio, 3),
            "monitors_per_75_lines": round(total_monitors / len(records), 2) if records else None,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

monitor_store = MonitorStore()
