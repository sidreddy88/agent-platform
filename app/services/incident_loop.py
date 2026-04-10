"""
IncidentLoop — drains the EventQueue through the TriageAgent.

Flow per event:
  1. Create IncidentState (status=TRIAGING)
  2. Run TriageAgent → TriageResult
  3. Update state: status, severity, blast_radius, occurrences_24h
  4. Slack notification via AlertingService
  5. Hand off to Diagnosis Agent (Week 2 — status set to DIAGNOSING for real incidents)

Noise → NOISE (terminal)
Duplicate → DUPLICATE (terminal, PR linked)
Real → DIAGNOSING (diagnosis agent picks up from here)
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.agents.triage import TriageAgent, TriageResult
from app.models.events import ErrorEvent, IncidentStatus, Severity
from app.services.alerting import Alert, alerting_service
from app.services.alerting import Severity as AlertSeverity
from app.services.event_queue import event_queue
from app.services.incident_store import incident_store

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

def _alert_severity(decision: str, severity: str) -> AlertSeverity:
    if decision in ("noise", "duplicate"):
        return AlertSeverity.INFO
    return {
        "P0": AlertSeverity.CRITICAL,
        "P1": AlertSeverity.ERROR,
        "P2": AlertSeverity.WARNING,
        "P3": AlertSeverity.INFO,
    }.get(severity, AlertSeverity.WARNING)


async def _notify(incident_id: str, event: ErrorEvent, result: TriageResult) -> None:
    if result.decision == "noise":
        title = f"[NOISE] {event.title}"
        message = f"Triaged as noise — standing down.\nReason: {result.reasoning}"
    elif result.decision == "duplicate":
        title = f"[DUPLICATE] {event.title}"
        message = (
            f"Open PR already covers this — standing down.\n"
            f"PR: {result.duplicate_pr}\n"
            f"Reason: {result.reasoning}"
        )
    else:
        title = f"[{result.severity}] {event.title}"
        message = (
            f"*Service:* {event.service}\n"
            f"*Occurrences (24h):* {result.occurrences_24h}\n"
            f"*Blast radius:* {result.blast_radius}\n"
            f"*Reason:* {result.reasoning}\n"
            f"*Incident:* {incident_id}\n"
            "Proceeding to diagnosis."
        )

    await alerting_service.send_alert(Alert(
        severity=_alert_severity(result.decision, result.severity),
        title=title,
        message=message,
        source="TriageAgent",
        metadata={"incident_id": incident_id, "decision": result.decision},
    ))


# ---------------------------------------------------------------------------
# IncidentLoop
# ---------------------------------------------------------------------------

class IncidentLoop:
    """Background worker that consumes ErrorEvents and runs the TriageAgent."""

    def __init__(self) -> None:
        self._running = False
        self._triage = TriageAgent()

    async def _process(self, event: ErrorEvent) -> None:
        incident = incident_store.create(event)
        incident.status = IncidentStatus.TRIAGING
        incident_store.update(incident)
        logger.info("[IncidentLoop] Triaging %s — %s", incident.id, event.title)

        try:
            result = await self._triage.triage(event)
        except Exception as exc:
            logger.error("[IncidentLoop] TriageAgent failed for %s: %s", incident.id, exc)
            # Fallback: treat as real P2 so we never silently drop an event
            result = TriageResult(
                decision="real",
                severity="P2",
                blast_radius="unknown",
                occurrences_24h=0,
                duplicate_pr=None,
                reasoning=f"Triage failed ({exc}) — defaulting to real/P2",
            )

        # Update incident state
        incident.triage_decision = result.decision
        incident.triage_reasoning = result.reasoning
        incident.blast_radius = result.blast_radius
        incident.occurrences_24h = result.occurrences_24h
        incident.triage_completed_at = datetime.utcnow()

        if result.decision == "duplicate":
            incident.status = IncidentStatus.DUPLICATE
            incident.pr_url = result.duplicate_pr
        elif result.decision == "noise":
            incident.status = IncidentStatus.NOISE
        else:
            # Set severity from triage onto the event, then hand off
            try:
                incident.error_event.severity = Severity[result.severity]
            except KeyError:
                incident.error_event.severity = Severity.P2
            incident.status = IncidentStatus.DIAGNOSING  # Diagnosis Agent picks up here (Week 2)

        incident_store.update(incident)
        logger.info(
            "[IncidentLoop] %s → decision=%s severity=%s occurrences=%d",
            incident.id, result.decision, result.severity, result.occurrences_24h,
        )

        await _notify(incident.id, event, result)

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
