"""
Agent status tracker — registry of active and recent agent runs.

Active runs are kept in memory. Completed and failed runs are persisted
to SQLite (agent_platform.db) and loaded back on startup, so run history
and per-agent stats survive server restarts.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AgentRun:
    run_id: str
    agent_name: str
    incident_id: str | None
    status: str  # "running" | "completed" | "failed"
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: float | None = None
    error_message: str | None = None
    tool_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "agent_name": self.agent_name,
            "incident_id": self.incident_id,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "duration_ms": self.duration_ms,
            "error_message": self.error_message,
            "tool_calls": self.tool_calls,
        }


class AgentStatusTracker:
    """Singleton tracking all agent invocations."""

    def __init__(self) -> None:
        self._active: dict[str, AgentRun] = {}
        self._recent_errors: deque[AgentRun] = deque(maxlen=20)
        self._history: deque[AgentRun] = deque(maxlen=500)
        self._load_from_db()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, agent_name: str, incident_id: str | None = None) -> str:
        """Register a new run. Returns the run_id."""
        run_id = uuid.uuid4().hex[:8]
        run = AgentRun(
            run_id=run_id,
            agent_name=agent_name,
            incident_id=incident_id,
            status="running",
            started_at=datetime.utcnow(),
        )
        self._active[run_id] = run
        return run_id

    def increment_tool_call(self, run_id: str) -> None:
        if run_id in self._active:
            self._active[run_id].tool_calls += 1

    def complete(self, run_id: str) -> AgentRun | None:
        run = self._active.pop(run_id, None)
        if run:
            run.status = "completed"
            run.completed_at = datetime.utcnow()
            run.duration_ms = (run.completed_at - run.started_at).total_seconds() * 1000
            self._history.append(run)
            self._persist_run(run)
        return run

    def fail(self, run_id: str, error: str) -> AgentRun | None:
        run = self._active.pop(run_id, None)
        if run:
            run.status = "failed"
            run.completed_at = datetime.utcnow()
            run.duration_ms = (run.completed_at - run.started_at).total_seconds() * 1000
            run.error_message = error
            self._recent_errors.appendleft(run)
            self._history.append(run)
            self._persist_run(run)
        return run

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def active_runs(self) -> list[AgentRun]:
        return list(self._active.values())

    def recent_errors(self) -> list[AgentRun]:
        return list(self._recent_errors)

    def pipeline_activity(self, window_seconds: int = 30) -> list[AgentRun]:
        """Active runs + runs completed within the last N seconds, newest first."""
        now = datetime.utcnow()
        recent = [
            r for r in self._history
            if r.completed_at and (now - r.completed_at).total_seconds() <= window_seconds
        ]
        combined = list(self._active.values()) + recent
        combined.sort(key=lambda r: r.started_at, reverse=True)
        return combined

    def stats(self) -> list[dict[str, Any]]:
        """Per-agent stats for runs today (completed + failed)."""
        today = date.today()
        by_agent: dict[str, list[AgentRun]] = defaultdict(list)

        for run in self._history:
            if run.started_at.date() == today:
                by_agent[run.agent_name].append(run)

        all_names = set(by_agent.keys()) | {r.agent_name for r in self._active.values()}

        result = []
        for name in sorted(all_names):
            today_runs = by_agent.get(name, [])
            errors = [r for r in today_runs if r.status == "failed"]
            durations = [r.duration_ms for r in today_runs if r.duration_ms is not None]
            active_count = sum(1 for r in self._active.values() if r.agent_name == name)
            result.append({
                "agent_name": name,
                "runs_today": len(today_runs),
                "errors_today": len(errors),
                "error_rate": round(len(errors) / len(today_runs), 3) if today_runs else 0.0,
                "avg_duration_ms": round(sum(durations) / len(durations)) if durations else None,
                "currently_active": active_count,
            })
        return result

    def get_runs_for_incident(self, incident_id: str) -> list[dict[str, Any]]:
        """Return all completed/failed runs for an incident from DB, oldest first."""
        from app.services.database import get_db
        try:
            conn = get_db()
            try:
                rows = list(conn.execute(
                    "SELECT * FROM agent_runs WHERE incident_id = ? ORDER BY started_at ASC",
                    (incident_id,),
                ))
            finally:
                conn.close()
            return [
                {
                    "run_id": row["run_id"],
                    "agent_name": row["agent_name"],
                    "incident_id": row["incident_id"],
                    "status": row["status"],
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "duration_ms": row["duration_ms"],
                    "error_message": row["error_message"],
                    "tool_calls": row["tool_calls"] or 0,
                }
                for row in rows
            ]
        except Exception as exc:
            logger.warning("[AgentTracker] get_runs_for_incident failed: %s", exc)
            return []

    def snapshot(self) -> dict[str, Any]:
        raw = {
            "active_runs": [r.to_dict() for r in self.active_runs()],
            "pipeline_activity": [r.to_dict() for r in self.pipeline_activity()],
            "recent_errors": [r.to_dict() for r in self.recent_errors()],
            "stats": self.stats(),
        }
        return json.loads(json.dumps(raw, default=str))

    # ------------------------------------------------------------------
    # DB helpers
    # ------------------------------------------------------------------

    def _persist_run(self, run: AgentRun) -> None:
        from app.services.database import get_db
        try:
            conn = get_db()
            try:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO agent_runs
                        (run_id, agent_name, incident_id, status, started_at,
                         completed_at, duration_ms, error_message, tool_calls)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id,
                        run.agent_name,
                        run.incident_id,
                        run.status,
                        run.started_at.isoformat(),
                        run.completed_at.isoformat() if run.completed_at else None,
                        run.duration_ms,
                        run.error_message,
                        run.tool_calls,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("[AgentTracker] DB write failed: %s", exc)

    def _load_from_db(self) -> None:
        from app.services.database import get_db
        try:
            conn = get_db()
            try:
                rows = list(conn.execute(
                    "SELECT * FROM agent_runs ORDER BY started_at DESC LIMIT 500"
                ))
            finally:
                conn.close()

            # Load in chronological order into the history deque
            for row in reversed(rows):
                run = AgentRun(
                    run_id=row["run_id"],
                    agent_name=row["agent_name"],
                    incident_id=row["incident_id"],
                    status=row["status"],
                    started_at=datetime.fromisoformat(row["started_at"]),
                    completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
                    duration_ms=row["duration_ms"],
                    error_message=row["error_message"],
                    tool_calls=row["tool_calls"] or 0,
                )
                self._history.append(run)
                if run.status == "failed":
                    self._recent_errors.appendleft(run)

            if rows:
                logger.info("[AgentTracker] Loaded %d runs from DB", len(rows))
        except Exception as exc:
            logger.warning("[AgentTracker] DB load failed: %s", exc)


agent_tracker = AgentStatusTracker()
