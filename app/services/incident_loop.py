"""
IncidentLoop — sequential incident pipeline driven by the EventQueue.

Pipeline per event:
  1. TriageAgent   → real / noise / duplicate  (Haiku)
  2. DiagnosisAgent → root cause + confidence  (Sonnet)
  3. Confidence gate → FIXING (≥0.70) or AWAITING_APPROVAL (<0.70)
  4. [Week 3] Fix Generation Agent picks up FIXING incidents

Status flow:
  OPEN → TRIAGING → NOISE / DUPLICATE (terminal)
                  → DIAGNOSING → AWAITING_APPROVAL (low confidence)
                              → FIXING (high confidence — Week 3 hook)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.agents.diagnosis import CONFIDENCE_THRESHOLD, DiagnosisAgent, DiagnosisResult
from app.agents.triage import TriageAgent, TriageResult
from app.models.events import ErrorEvent, IncidentState, IncidentStatus, Severity
from app.services.alerting import Alert, alerting_service
from app.services.alerting import Severity as AlertSeverity
from app.services.event_queue import event_queue
from app.services.incident_store import incident_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def _p_to_alert_sev(severity: str) -> AlertSeverity:
    return {"P0": AlertSeverity.CRITICAL, "P1": AlertSeverity.ERROR,
            "P2": AlertSeverity.WARNING, "P3": AlertSeverity.INFO}.get(severity, AlertSeverity.WARNING)


async def _notify_triage(incident_id: str, event: ErrorEvent, result: TriageResult) -> None:
    if result.decision == "noise":
        title = f"[NOISE] {event.title}"
        message = f"Triaged as noise — standing down.\nReason: {result.reasoning}"
        sev = AlertSeverity.INFO
    elif result.decision == "duplicate":
        title = f"[DUPLICATE] {event.title}"
        message = f"Open PR already covers this — standing down.\nPR: {result.duplicate_pr}\nReason: {result.reasoning}"
        sev = AlertSeverity.INFO
    else:
        title = f"[{result.severity}] {event.title} — triaged"
        message = (
            f"*Service:* {event.service}  |  *Occurrences (24h):* {result.occurrences_24h}\n"
            f"*Blast radius:* {result.blast_radius}\n"
            f"*Reason:* {result.reasoning}\n"
            f"*Incident:* {incident_id} — proceeding to diagnosis"
        )
        sev = _p_to_alert_sev(result.severity)

    await alerting_service.send_alert(Alert(
        severity=sev, title=title, message=message,
        source="TriageAgent", metadata={"incident_id": incident_id, "decision": result.decision},
    ))


async def _notify_diagnosis(incident: IncidentState, result: DiagnosisResult) -> None:
    event = incident.error_event
    sev = _p_to_alert_sev(str(event.severity).split(".")[-1] if event.severity else "P2")

    if result.escalate:
        title = f"[LOW CONFIDENCE] {event.title} — human review needed"
        message = (
            f"*Confidence:* {result.confidence:.0%}  (threshold {CONFIDENCE_THRESHOLD:.0%})\n"
            f"*Root cause:* {result.root_cause}\n"
            f"*Evidence:* {', '.join(result.evidence[:2])}\n"
            f"*Reproduction:* {'✅ confirmed' if result.reproduction_confirmed else '❌ not confirmed'}\n"
            f"*Incident:* {incident.id} — awaiting human diagnosis"
        )
        sev = AlertSeverity.WARNING
    else:
        title = f"[{event.severity}] {event.title} — diagnosis complete"
        message = (
            f"*Confidence:* {result.confidence:.0%}\n"
            f"*Root cause:* {result.root_cause}\n"
            f"*Fix approach:* {result.fix_approach}\n"
            f"*Affected:* {result.affected_function or 'unknown'} in {result.affected_file or 'unknown'}\n"
            f"*Reproduction:* {'✅ confirmed' if result.reproduction_confirmed else '❌ not confirmed'}\n"
            f"*Incident:* {incident.id} — proceeding to fix generation"
        )

    await alerting_service.send_alert(Alert(
        severity=sev, title=title, message=message,
        source="DiagnosisAgent",
        metadata={"incident_id": incident.id, "confidence": result.confidence, "escalate": result.escalate},
    ))


# ---------------------------------------------------------------------------
# IncidentLoop
# ---------------------------------------------------------------------------

class IncidentLoop:
    """Sequential incident pipeline: Triage → Diagnosis → (Fix Generation — Week 3)."""

    def __init__(self) -> None:
        self._running = False
        self._triage = TriageAgent()
        self._diagnosis = DiagnosisAgent()

    # ------------------------------------------------------------------ #
    # Step 1 — Triage
    # ------------------------------------------------------------------ #

    async def _run_triage(self, event: ErrorEvent) -> TriageResult:
        try:
            return await self._triage.triage(event)
        except Exception as exc:
            logger.error("[IncidentLoop] TriageAgent failed: %s", exc)
            return TriageResult(
                decision="real", severity="P2", blast_radius="unknown",
                occurrences_24h=0, duplicate_pr=None,
                reasoning=f"Triage failed ({exc}) — defaulting to real/P2",
            )

    # ------------------------------------------------------------------ #
    # Step 2 — Diagnosis
    # ------------------------------------------------------------------ #

    async def _run_diagnosis(self, incident: IncidentState) -> DiagnosisResult:
        try:
            return await self._diagnosis.diagnose(incident)
        except Exception as exc:
            logger.error("[IncidentLoop] DiagnosisAgent failed: %s", exc)
            return DiagnosisResult(
                root_cause=f"Diagnosis failed ({exc}) — manual review required",
                confidence=0.0,
                escalate=True,
            )

    # ------------------------------------------------------------------ #
    # Main pipeline
    # ------------------------------------------------------------------ #

    async def _process(self, event: ErrorEvent) -> None:
        # ── Triage ────────────────────────────────────────────────────
        incident = incident_store.create(event)
        incident.status = IncidentStatus.TRIAGING
        incident_store.update(incident)
        logger.info("[IncidentLoop] Triaging %s — %s", incident.id, event.title)

        triage = await self._run_triage(event)

        incident.triage_decision = triage.decision
        incident.triage_reasoning = triage.reasoning
        incident.blast_radius = triage.blast_radius
        incident.occurrences_24h = triage.occurrences_24h
        incident.triage_completed_at = datetime.utcnow()

        if triage.decision == "duplicate":
            incident.status = IncidentStatus.DUPLICATE
            incident.pr_url = triage.duplicate_pr
            incident_store.update(incident)
            await _notify_triage(incident.id, event, triage)
            return

        if triage.decision == "noise":
            incident.status = IncidentStatus.NOISE
            incident_store.update(incident)
            await _notify_triage(incident.id, event, triage)
            return

        # Real incident — set severity and proceed
        try:
            incident.error_event.severity = Severity[triage.severity]
        except KeyError:
            incident.error_event.severity = Severity.P2
        incident.status = IncidentStatus.DIAGNOSING
        incident_store.update(incident)
        await _notify_triage(incident.id, event, triage)

        logger.info(
            "[IncidentLoop] %s triaged → %s/%s, occurrences=%d — running diagnosis",
            incident.id, triage.decision, triage.severity, triage.occurrences_24h,
        )

        # ── Diagnosis ─────────────────────────────────────────────────
        diagnosis = await self._run_diagnosis(incident)

        incident.diagnosis = diagnosis.root_cause
        incident.confidence = diagnosis.confidence
        incident.reproduction_confirmed = diagnosis.reproduction_confirmed
        incident.diagnosis_completed_at = datetime.utcnow()

        if diagnosis.escalate:
            incident.status = IncidentStatus.AWAITING_APPROVAL
            logger.warning(
                "[IncidentLoop] %s → low confidence (%.0f%%) — escalating to human",
                incident.id, diagnosis.confidence * 100,
            )
        else:
            incident.status = IncidentStatus.FIXING   # Fix Generation Agent — Week 3
            logger.info(
                "[IncidentLoop] %s → confidence %.0f%% — proceeding to fix generation",
                incident.id, diagnosis.confidence * 100,
            )

        incident_store.update(incident)
        await _notify_diagnosis(incident, diagnosis)

    async def run_forever(self) -> None:
        self._running = True
        logger.info("[IncidentLoop] Started — waiting for events")
        while self._running:
            try:
                event = await asyncio.wait_for(event_queue.dequeue(), timeout=5.0)
                asyncio.create_task(self._process(event))
                event_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.error("[IncidentLoop] Queue consumer error: %s", exc)

    def stop(self) -> None:
        self._running = False


# Module-level singleton
incident_loop = IncidentLoop()
