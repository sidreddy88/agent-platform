"""
AgentSessionLogger — writes a structured JSONL log of every agent run.

One JSON record per incident, appended to logs/agent_sessions.jsonl.
Each record captures the full pipeline: triage → diagnosis → fix (with
sandbox attempts) → PR outcome, plus a harness_compliance block that
shows whether agents read the required harness docs before working.

The harness_compliance fields are all False by default. They flip to True
only when an agent explicitly marks them (e.g. when harness doc injection
is added to FixGenerationAgent). This makes process gaps immediately visible
without any manual inspection.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOG_DIR = Path(__file__).parent.parent.parent / "logs"
LOG_FILE = LOG_DIR / "agent_sessions.jsonl"

_HARNESS_FILES = [
    "AGENTS.md",
    "CONSTRAINTS.md",
    "DECISIONS.md",
    "PROGRESS.md",
    "QUALITY.md",
]


class AgentSession:
    """
    Accumulates log events for a single incident run.
    Call flush() to write the completed record to disk.
    """

    def __init__(self, incident_id: str, error_title: str, error_type: str | None):
        self.incident_id = incident_id
        self.error_title = error_title
        self.error_type = error_type
        self.started_at = _now()
        self.ended_at: str | None = None
        self.outcome: str = "in_progress"

        # Harness compliance — all False until explicitly marked
        self.harness_compliance: dict[str, bool] = {
            "agents_md_read": False,
            "constraints_md_read": False,
            "decisions_md_read": False,
            "progress_md_updated": False,
            "sprint_contract_created": False,
            "exit_checklist_verified": False,
        }

        # Pipeline stages
        self.triage: dict[str, Any] | None = None
        self.diagnosis: dict[str, Any] | None = None
        self.fix: dict[str, Any] | None = None

        # Full step log (raw strings from fix_with_steps)
        self.steps: list[str] = []

    # ── Harness compliance ──────────────────────────────────────────────

    def mark_harness_file_read(self, filename: str) -> None:
        """Call this when an agent reads a harness doc before starting work."""
        key_map = {
            "AGENTS.md": "agents_md_read",
            "CONSTRAINTS.md": "constraints_md_read",
            "DECISIONS.md": "decisions_md_read",
            "PROGRESS.md": "progress_md_updated",
        }
        key = key_map.get(filename)
        if key:
            self.harness_compliance[key] = True

    def mark_sprint_contract_created(self) -> None:
        self.harness_compliance["sprint_contract_created"] = True

    def mark_exit_checklist_verified(self) -> None:
        self.harness_compliance["exit_checklist_verified"] = True

    # ── Pipeline stages ─────────────────────────────────────────────────

    def log_triage(
        self,
        decision: str,
        severity: str | None,
        confidence: float,
        reasoning: str,
        occurrences_24h: int,
    ) -> None:
        self.triage = {
            "decision": decision,
            "severity": severity,
            "confidence": round(confidence, 3),
            "reasoning": reasoning[:300],
            "occurrences_24h": occurrences_24h,
        }

    def log_diagnosis(
        self,
        root_cause: str,
        confidence: float,
        fix_approach: str,
        escalated: bool,
        raw_llm: str = "",
    ) -> None:
        self.diagnosis = {
            "root_cause": root_cause[:500],
            "confidence": round(confidence, 3),
            "fix_approach": fix_approach[:300],
            "escalated": escalated,
            "raw_llm": raw_llm[:2000] if raw_llm else None,
        }

    def log_fix_start(self, target_file: str | None, target_function: str | None) -> None:
        self.fix = {
            "target_file": target_file,
            "target_function": target_function,
            "sandbox_attempts": [],
            "pr_url": None,
            "pr_number": None,
            "blast_radius_violation": False,
            "failure_reason": None,
        }

    def update_fix_target(self, target_file: str | None, target_function: str | None) -> None:
        if self.fix is not None:
            self.fix["target_file"] = target_file
            self.fix["target_function"] = target_function

    def log_sandbox_attempt(
        self,
        attempt: int,
        passed: bool,
        output_tail: str,
        fix_content: str | None = None,
    ) -> None:
        if self.fix is None:
            self.fix = {"sandbox_attempts": []}
        entry: dict = {
            "attempt": attempt,
            "passed": passed,
            "output_tail": output_tail[-400:] if output_tail else "",
        }
        if fix_content:
            entry["fix_content"] = fix_content[:4000]
        self.fix["sandbox_attempts"].append(entry)

    def log_fix_outcome(
        self,
        pr_url: str | None,
        pr_number: int | None,
        blast_radius_violation: bool,
        failure_reason: str | None,
    ) -> None:
        if self.fix is None:
            self.fix = {"sandbox_attempts": []}
        self.fix.update({
            "pr_url": pr_url,
            "pr_number": pr_number,
            "blast_radius_violation": blast_radius_violation,
            "failure_reason": failure_reason,
        })

    def log_steps(self, steps: list[str]) -> None:
        self.steps = steps

    # ── Finalise ─────────────────────────────────────────────────────────

    def complete(self, outcome: str) -> None:
        self.outcome = outcome
        self.ended_at = _now()

    def to_dict(self) -> dict:
        return {
            "incident_id": self.incident_id,
            "error_title": self.error_title,
            "error_type": self.error_type,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "outcome": self.outcome,
            "harness_compliance": self.harness_compliance,
            "pipeline": {
                "triage": self.triage,
                "diagnosis": self.diagnosis,
                "fix": self.fix,
            },
            "steps": self.steps,
        }


class SessionLogger:
    """
    Singleton that manages in-flight AgentSessions and flushes completed
    ones to logs/agent_sessions.jsonl.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}
        self._lock = threading.Lock()

    def start(self, incident_id: str, error_title: str, error_type: str | None = None) -> AgentSession:
        session = AgentSession(incident_id, error_title, error_type)
        with self._lock:
            self._sessions[incident_id] = session
        logger.debug("[SessionLogger] Started session for %s", incident_id)
        return session

    def get(self, incident_id: str) -> AgentSession | None:
        return self._sessions.get(incident_id)

    def finish(self, incident_id: str, outcome: str) -> None:
        with self._lock:
            session = self._sessions.pop(incident_id, None)
        if session is None:
            return
        session.complete(outcome)
        self._write(session)

    def _write(self, session: AgentSession) -> None:
        try:
            LOG_DIR.mkdir(exist_ok=True)
            with LOG_FILE.open("a") as f:
                f.write(json.dumps(session.to_dict()) + "\n")
            logger.info(
                "[SessionLogger] %s → outcome=%s written to %s",
                session.incident_id, session.outcome, LOG_FILE,
            )
        except Exception as exc:
            logger.warning("[SessionLogger] Failed to write session log: %s", exc)

    def recent(self, n: int = 20) -> list[dict]:
        """Return the n most recent completed session records from disk."""
        if not LOG_FILE.exists():
            return []
        lines = LOG_FILE.read_text().strip().splitlines()
        records = []
        for line in reversed(lines[-max(n * 2, 100):]):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
            if len(records) >= n:
                break
        return records


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Module-level singleton
session_logger = SessionLogger()
