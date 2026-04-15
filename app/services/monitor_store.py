"""
MonitorStore — SQLite-backed store for monitor generation results.

Holds the last 100 MonitorGenerationResult objects (one per PR merge) in
memory for fast reads, with full history persisted to agent_platform.db.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.agents.monitor_generation import MonitorGenerationResult


class MonitorStore:
    def __init__(self, maxlen: int = 100) -> None:
        self._records: deque[dict[str, Any]] = deque(maxlen=maxlen)
        self._load_from_db()

    def save(self, repo: str, pr_number: int, result: "MonitorGenerationResult") -> None:
        record = {
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
        }
        self._records.append(record)
        self._persist_record(record)

    def get_all(self) -> list[dict[str, Any]]:
        return list(reversed(self._records))

    def coverage_metrics(self) -> dict[str, Any]:
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

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _persist_record(self, record: dict[str, Any]) -> None:
        from app.services.database import get_db
        try:
            conn = get_db()
            try:
                conn.execute(
                    "INSERT INTO monitor_records (repo, pr_number, generated_at, data) VALUES (?, ?, ?, ?)",
                    (record["repo"], record["pr_number"], record["generated_at"], json.dumps(record)),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("[MonitorStore] DB write failed: %s", exc)

    def _load_from_db(self) -> None:
        from app.services.database import get_db
        try:
            conn = get_db()
            try:
                rows = list(conn.execute(
                    "SELECT data FROM monitor_records ORDER BY id DESC LIMIT 100"
                ))
            finally:
                conn.close()
            # Load in chronological order (oldest first) into the deque
            for row in reversed(rows):
                self._records.append(json.loads(row["data"]))
            if rows:
                logger.info("[MonitorStore] Loaded %d records from DB", len(rows))
        except Exception as exc:
            logger.warning("[MonitorStore] DB load failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

monitor_store = MonitorStore()
