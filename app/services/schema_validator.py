"""
Schema validation between agent handoffs.

Validates (and coerces where safe) the typed dataclass outputs produced by
each agent before they are written onto IncidentState or passed to the next
agent.  Raises HandoffValidationError on unrecoverable violations so that
the existing try/except fallbacks in IncidentLoop degrade gracefully rather
than silently propagating bad data through the pipeline.

Validation points:
  1. TriageResult       → IncidentState
  2. DiagnosisResult    → IncidentState
  3. FixResult          → CodeReviewAgent  (pre-review guard)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Violation record + exception
# ---------------------------------------------------------------------------

@dataclass
class SchemaViolation:
    field: str
    value: Any
    message: str

    def __str__(self) -> str:
        return f"{self.field}={self.value!r}: {self.message}"


class HandoffValidationError(ValueError):
    """Raised when agent output fails schema validation at a handoff boundary."""

    def __init__(self, stage: str, violations: list[SchemaViolation]) -> None:
        self.stage = stage
        self.violations = violations
        detail = "; ".join(str(v) for v in violations)
        super().__init__(f"[{stage}] schema violation — {detail}")


# ---------------------------------------------------------------------------
# HandoffValidator
# ---------------------------------------------------------------------------

class HandoffValidator:
    """
    Validates agent outputs at handoff boundaries.

    Each validate_* method:
      - Coerces case/whitespace where the intent is clear.
      - Logs a warning for every coercion.
      - Raises HandoffValidationError if any field is unrecoverable.
      - Returns the (possibly coerced) result on success.
    """

    # ---- allowed value sets ----
    _TRIAGE_DECISIONS = {"real", "noise", "duplicate"}
    _SEVERITIES       = {"P0", "P1", "P2", "P3"}
    _BLAST_RADII      = {"single_service", "multi_service", "unknown"}

    # ------------------------------------------------------------------
    # 1. TriageResult → IncidentState
    # ------------------------------------------------------------------

    def validate_triage(self, result: Any) -> Any:
        """
        Validate TriageResult before its fields are written to IncidentState.

        Coerces:
          decision    → strip + lower  (e.g. "Real" → "real")
          severity    → strip + upper  (e.g. "p1"  → "P1")
          blast_radius → strip + lower

        Raises HandoffValidationError if any field is outside allowed values
        after coercion, or if reasoning is empty.
        """
        violations: list[SchemaViolation] = []
        coercions: list[str] = []

        # --- decision ---
        raw_decision = (result.decision or "").strip()
        decision = raw_decision.lower()
        if decision != raw_decision:
            coercions.append(f"decision {raw_decision!r} → {decision!r}")
        if decision not in self._TRIAGE_DECISIONS:
            violations.append(SchemaViolation(
                "decision", result.decision,
                f"must be one of {sorted(self._TRIAGE_DECISIONS)}, got {result.decision!r}",
            ))

        # --- severity ---
        raw_severity = (result.severity or "").strip()
        severity = raw_severity.upper()
        if severity != raw_severity:
            coercions.append(f"severity {raw_severity!r} → {severity!r}")
        if severity not in self._SEVERITIES:
            violations.append(SchemaViolation(
                "severity", result.severity,
                f"must be one of {sorted(self._SEVERITIES)}, got {result.severity!r}",
            ))

        # --- blast_radius ---
        raw_br = (result.blast_radius or "").strip()
        blast_radius = raw_br.lower()
        if blast_radius != raw_br:
            coercions.append(f"blast_radius {raw_br!r} → {blast_radius!r}")
        if blast_radius not in self._BLAST_RADII:
            violations.append(SchemaViolation(
                "blast_radius", result.blast_radius,
                f"must be one of {sorted(self._BLAST_RADII)}, got {result.blast_radius!r}",
            ))

        # --- occurrences_24h ---
        if result.occurrences_24h < 0:
            violations.append(SchemaViolation(
                "occurrences_24h", result.occurrences_24h,
                "must be >= 0",
            ))

        # --- reasoning ---
        if not (result.reasoning or "").strip():
            violations.append(SchemaViolation(
                "reasoning", result.reasoning,
                "must be a non-empty string",
            ))

        if violations:
            logger.error(
                "[SchemaValidator] triage→incident violations: %s",
                "; ".join(str(v) for v in violations),
            )
            raise HandoffValidationError("triage→incident", violations)

        if coercions:
            logger.warning(
                "[SchemaValidator] triage→incident coercions applied: %s",
                ", ".join(coercions),
            )

        return replace(result, decision=decision, severity=severity, blast_radius=blast_radius)

    # ------------------------------------------------------------------
    # 2. DiagnosisResult → IncidentState
    # ------------------------------------------------------------------

    def validate_diagnosis(self, result: Any) -> Any:
        """
        Validate DiagnosisResult before its fields are written to IncidentState.

        Raises HandoffValidationError if:
          - root_cause is empty
          - confidence is outside [0.0, 1.0]
        """
        violations: list[SchemaViolation] = []

        # --- root_cause ---
        if not (result.root_cause or "").strip():
            violations.append(SchemaViolation(
                "root_cause", result.root_cause,
                "must be a non-empty string",
            ))

        # --- confidence ---
        conf = result.confidence
        if conf is None or not (0.0 <= conf <= 1.0):
            violations.append(SchemaViolation(
                "confidence", conf,
                "must be a float in [0.0, 1.0]",
            ))

        if violations:
            logger.error(
                "[SchemaValidator] diagnosis→incident violations: %s",
                "; ".join(str(v) for v in violations),
            )
            raise HandoffValidationError("diagnosis→incident", violations)

        return result

    # ------------------------------------------------------------------
    # 3. FixResult → CodeReviewAgent
    # ------------------------------------------------------------------

    def validate_fix_for_review(self, result: Any) -> None:
        """
        Validate FixResult before CodeReviewAgent is invoked.

        Raises HandoffValidationError if pr_number or pr_url are missing
        (both are required for a meaningful code review).
        """
        violations: list[SchemaViolation] = []

        if result.pr_number is None:
            violations.append(SchemaViolation(
                "pr_number", None,
                "must be set before passing to CodeReviewAgent",
            ))

        if not result.pr_url:
            violations.append(SchemaViolation(
                "pr_url", result.pr_url,
                "must be set before passing to CodeReviewAgent",
            ))

        if violations:
            logger.error(
                "[SchemaValidator] fix→review violations: %s",
                "; ".join(str(v) for v in violations),
            )
            raise HandoffValidationError("fix→review", violations)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

handoff_validator = HandoffValidator()
