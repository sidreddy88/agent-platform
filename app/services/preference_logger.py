"""
RLHF preference-pair logger.

Every time a human rejects an AI-generated fix, the full context is
recorded as a negative training example in JSONL format.  These pairs
can be fed directly into fine-tuning or preference-optimisation pipelines
(RLHF / DPO / Constitutional AI) to teach the model what NOT to produce.

Record layout (one JSON object per line):
  {
    "id":           "pref_<8-char uuid fragment>",
    "type":         "rejection",
    "timestamp":    "2026-04-12T10:00:00Z",
    "incident_id":  "inc_...",

    "error_type":   "S3_NO_SUCH_KEY",
    "service":      "image-service",
    "severity":     "P2",

    "prompt": {
      "diagnosis":         "root cause text",
      "confidence":        0.85,
      "triage_reasoning":  "...",
      "triage_decision":   "real",
      "blast_radius":      "single_service"
    },

    "rejected_response": {
      "pr_url":            "https://github.com/org/repo/pull/42",
      "pr_number":         42,
      "fix_description":   "Changed s3_utils.py line 87 ..."
    },

    "rejection": {
      "reason":    "Wrong function modified — should touch upload_handler",
      "approver":  "alice"
    }
  }

File location: .preference_pairs.jsonl  (gitignored, never committed)

Usage (automatic — no manual calls needed):
    from app.services.preference_logger import preference_logger
    preference_logger.log_rejection(incident, approver="alice", reason="wrong fix")
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.models.events import IncidentState

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path(".preference_pairs.jsonl")


# ---------------------------------------------------------------------------
# PreferenceLogger
# ---------------------------------------------------------------------------

class PreferenceLogger:
    """
    Appends one JSONL record per rejected fix.

    Thread/asyncio safe for a single-process server (file appends on most
    POSIX filesystems are atomic for writes < PIPE_BUF ≈ 4 KB).
    """

    def __init__(self, path: Path | str = _DEFAULT_PATH) -> None:
        self._path = Path(path)
        self.logged_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_rejection(
        self,
        incident: IncidentState,
        approver: str,
        reason: str,
    ) -> dict[str, Any]:
        """
        Build and persist a rejection preference pair.

        Returns the dict that was written (for callers that want to
        surface it in API responses or tests).
        """
        pair = self._build_pair(incident, approver, reason)
        self._write(pair)
        self.logged_count += 1
        logger.info(
            "[PreferenceLogger] Logged rejection pair %s for incident %s (total: %d)",
            pair["id"], incident.id, self.logged_count,
        )
        return pair

    def get_all(self) -> list[dict[str, Any]]:
        """Return all recorded preference pairs, oldest first."""
        if not self._path.exists():
            return []
        pairs: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        pairs.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning("[PreferenceLogger] Skipping malformed line in %s", self._path)
        return pairs

    def get_rejections_for_incident(self, incident_id: str) -> list[dict[str, Any]]:
        """Return all rejection pairs for a specific incident."""
        return [p for p in self.get_all() if p.get("incident_id") == incident_id]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_pair(
        self,
        incident: IncidentState,
        approver: str,
        reason: str,
    ) -> dict[str, Any]:
        event = incident.error_event
        severity = str(event.severity).split(".")[-1] if event.severity else "unknown"

        return {
            "id": f"pref_{uuid.uuid4().hex[:8]}",
            "type": "rejection",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "incident_id": incident.id,
            "error_type": event.error_type,
            "service": event.service,
            "severity": severity,
            "prompt": {
                "diagnosis":        incident.diagnosis or "",
                "confidence":       incident.confidence,
                "triage_reasoning": incident.triage_reasoning or "",
                "triage_decision":  incident.triage_decision or "",
                "blast_radius":     incident.blast_radius or "",
            },
            "rejected_response": {
                "pr_url":          incident.pr_url or "",
                "pr_number":       incident.pr_number,
                "fix_description": incident.fix_description or incident.fix_attempted or "",
            },
            "rejection": {
                "reason":   reason,
                "approver": approver,
            },
        }

    def _write(self, pair: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(pair, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

preference_logger = PreferenceLogger()
