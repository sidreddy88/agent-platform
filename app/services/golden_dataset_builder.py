"""
GoldenDatasetBuilder — auto-captures real incident traces into the eval corpus.

Every time an incident reaches a terminal state (RESOLVED, REJECTED, NOISE,
DUPLICATE, or low-confidence AWAITING_APPROVAL), the full trace is appended
to app/evals/golden_dataset.jsonl in a format compatible with EvalRunner.

This grows the eval corpus from 10 hand-crafted cases to 50-100+ real traces,
establishing baseline eval scores for production incidents.

Quality filters:
  - Always capture: human-adjudicated incidents (ground truth)
  - Always capture: duplicate triage decisions (idempotency eval cases)
  - Conditionally capture: noise decisions (only if error_type is new)
  - Conditionally capture: auto runs with confidence >= 0.60 and unique error_type
  - Skip: low-signal runs (confidence < 0.60, no human decision)

Record format (appended to golden_dataset.jsonl):
  Standard fields: id, description, input, expected, tags   ← EvalRunner-compatible
  Extra field:     full_trace                                ← full incident state

Usage (automatic — called from incident_loop.py and approvals.py):
    from app.services.golden_dataset_builder import golden_dataset_builder
    golden_dataset_builder.capture(incident)
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

_DEFAULT_PATH = Path(__file__).parent.parent / "evals" / "golden_dataset.jsonl"
_CONFIDENCE_THRESHOLD = 0.60


class GoldenDatasetBuilder:
    """
    Appends incident traces to app/evals/golden_dataset.jsonl.

    Thread/asyncio safe for single-process use (file appends are atomic
    for writes < PIPE_BUF ≈ 4 KB on POSIX filesystems).
    """

    def __init__(self, path: Path | str = _DEFAULT_PATH) -> None:
        self._path = Path(path)
        self.captured_count: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def capture(self, incident: "IncidentState") -> dict[str, Any] | None:
        """
        Capture a terminal-state incident as a golden dataset record.

        Returns the written dict, or None if the incident was filtered out.
        """
        if not self._should_capture(incident):
            return None

        record = self._build_record(incident)
        self._write(record)
        self.captured_count += 1
        conf_str = f"{incident.confidence:.0%}" if incident.confidence is not None else "n/a"
        logger.info(
            "[GoldenDataset] Captured %s (decision=%s, confidence=%s, total=%d)",
            incident.id,
            incident.triage_decision,
            conf_str,
            self.captured_count,
        )
        return record

    def get_all(self) -> list[dict[str, Any]]:
        """Return all records in the dataset (hand-crafted + auto-captured), oldest first."""
        if not self._path.exists():
            return []
        records: list[dict[str, Any]] = []
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        logger.warning(
                            "[GoldenDataset] Skipping malformed line in %s", self._path
                        )
        return records

    def count(self) -> int:
        """Return the number of auto-captured records (id starts with 'auto_')."""
        return sum(1 for r in self.get_all() if r.get("id", "").startswith("auto_"))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _should_capture(self, incident: "IncidentState") -> bool:
        """Quality filter — decide whether this incident is worth capturing."""
        # Always capture human-adjudicated outcomes — these are ground truth
        if incident.human_decision is not None:
            return True

        # Always capture duplicates — useful for idempotency eval coverage
        if incident.triage_decision == "duplicate":
            return True

        # For noise decisions: only capture if this error_type is new to the dataset
        if incident.triage_decision == "noise":
            error_type = incident.error_event.error_type or ""
            return bool(error_type) and error_type not in self._existing_error_types()

        # For real incidents without human decision: require confidence >= threshold
        if incident.confidence is not None and incident.confidence < _CONFIDENCE_THRESHOLD:
            return False

        # Deduplicate by error_type to keep dataset diverse
        error_type = incident.error_event.error_type or ""
        if error_type and error_type in self._existing_error_types():
            return False

        return True

    def _existing_error_types(self) -> set[str]:
        """Read existing dataset to find all error_type values already present."""
        types: set[str] = set()
        for record in self.get_all():
            et = record.get("input", {}).get("error_type") or record.get("error_type", "")
            if et:
                types.add(et)
        return types

    def _build_record(self, incident: "IncidentState") -> dict[str, Any]:
        event = incident.error_event
        severity_str = str(event.severity).split(".")[-1] if event.severity else "P2"
        source_str = str(event.source).split(".")[-1] if event.source else "application"

        return {
            # Standard EvalRunner-compatible fields
            "id": f"auto_{uuid.uuid4().hex[:8]}",
            "description": f"{event.error_type or event.title} in {event.service}",
            "input": {
                "error_type": event.error_type or "",
                "title": event.title,
                "description": event.description,
                "service": event.service,
                "source": source_str,
            },
            "expected": {
                "triage_decision": incident.triage_decision or "real",
                "triage_severity": [severity_str],
            },
            "tags": ["auto-captured", incident.triage_decision or "unknown"],
            # Full trace for richer evals and debugging
            "full_trace": {
                "triage_reasoning": incident.triage_reasoning,
                "blast_radius": incident.blast_radius,
                "occurrences_24h": incident.occurrences_24h,
                "diagnosis": incident.diagnosis,
                "confidence": incident.confidence,
                "reproduction_confirmed": incident.reproduction_confirmed,
                "fix_attempted": incident.fix_attempted,
                "pr_url": incident.pr_url,
                "pr_number": incident.pr_number,
                "human_decision": incident.human_decision,
                "human_decision_reason": incident.human_decision_reason,
                "outcome": incident.outcome,
                "mttr_seconds": incident.mttr_seconds,
            },
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    def _write(self, record: dict[str, Any]) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

golden_dataset_builder = GoldenDatasetBuilder()
