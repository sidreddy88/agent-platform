"""
Agent status tracker — in-memory registry of active and recent agent runs.

Tracks which agents are currently executing, recent failures, and per-agent
stats (runs today, error rate, avg duration). Updated by BaseAgent.run().
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any


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
        # Runs currently in flight
        self._active: dict[str, AgentRun] = {}
        # Last 20 failed runs for error feed
        self._recent_errors: deque[AgentRun] = deque(maxlen=20)
        # Last 500 completed (or failed) runs for stats
        self._history: deque[AgentRun] = deque(maxlen=500)

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
        # Combine active (in-flight) + recent completed, sorted by start time desc
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

        # Ensure agents currently active appear even if no history today
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

    def snapshot(self) -> dict[str, Any]:
        raw = {
            "active_runs": [r.to_dict() for r in self.active_runs()],
            "pipeline_activity": [r.to_dict() for r in self.pipeline_activity()],
            "recent_errors": [r.to_dict() for r in self.recent_errors()],
            "stats": self.stats(),
        }
        # Guarantee JSON safety — converts any stray datetime/enum/etc. to str
        return json.loads(json.dumps(raw, default=str))


agent_tracker = AgentStatusTracker()
