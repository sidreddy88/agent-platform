"""
IncidentLoop — sequential incident pipeline driven by the EventQueue.

Pipeline per event:
  1. TriageAgent        → real / noise / duplicate  (Haiku)
  2. DiagnosisAgent     → root cause + confidence   (Sonnet)
  3. Confidence gate    → FIXING (≥0.70) or AWAITING_APPROVAL (<0.70)
  4. FixGenerationAgent → reads file, creates Issue + PR
  5. CodeReviewAgent    → posts AI review to the PR
  6. Human approval     → Slack message with approve/reject links

Status flow:
  OPEN → TRIAGING → NOISE / DUPLICATE (terminal)
                  → DIAGNOSING → AWAITING_APPROVAL (low confidence, terminal)
                              → FIXING → REVIEWING → AWAITING_APPROVAL (human gate)
                                                   → RESOLVED / REJECTED
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.agents.code_review import CodeReviewAgent
from app.agents.diagnosis import CONFIDENCE_THRESHOLD, DiagnosisAgent, DiagnosisResult
from app.agents.fix_generation import FixGenerationAgent, FixResult
from app.agents.triage import TriageAgent, TriageResult
from app.core.config import settings
from app.models.events import ErrorEvent, IncidentState, IncidentStatus, Severity
from app.services.alerting import Alert, alerting_service
from app.services.alerting import Severity as AlertSeverity
from app.services.approvals import RiskLevel, approval_service
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


async def _notify_fix_ready(incident: IncidentState, fix: FixResult, approval_id: str) -> None:
    event = incident.error_event
    sev_str = str(event.severity).split(".")[-1] if event.severity else "P2"
    approve_url = f"{settings.approval_base_url}/approvals/{approval_id}/approve"
    reject_url = f"{settings.approval_base_url}/approvals/{approval_id}/reject"

    message = (
        f"*Service:* {event.service}  |  *Severity:* {sev_str}\n"
        f"*Confidence:* {incident.confidence:.0%}  |  *Occurrences (24h):* {incident.occurrences_24h}\n"
        f"*Root cause:* {incident.diagnosis}\n"
        f"*PR:* {fix.pr_url or '(pending)'}\n"
        f"*Branch:* {fix.branch}\n"
        f"*Files changed:* {', '.join(fix.files_changed) or 'none'}\n"
        f"*Test added:* {'yes' if fix.test_added else 'no'}\n\n"
        f"<{approve_url}|✅ Approve merge>  |  <{reject_url}|❌ Reject fix>\n"
        f"_Approval ID: {approval_id}_"
    )
    await alerting_service.send_alert(Alert(
        severity=AlertSeverity.WARNING,
        title=f"[{sev_str}] AI fix ready — PR #{fix.pr_number} | Human approval needed",
        message=message,
        source="FixGenerationAgent",
        metadata={
            "incident_id": incident.id,
            "pr_url": fix.pr_url,
            "approval_id": approval_id,
        },
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
    """Sequential incident pipeline: Triage → Diagnosis → Fix → Review → Approval."""

    def __init__(self) -> None:
        self._running = False
        self._triage = TriageAgent()
        self._diagnosis = DiagnosisAgent()
        self._fix_agent = FixGenerationAgent()
        self._review_agent = CodeReviewAgent()

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

    async def _run_fix(self, incident: IncidentState) -> FixResult | None:
        try:
            return await self._fix_agent.fix(incident)
        except Exception as exc:
            logger.error("[IncidentLoop] FixGenerationAgent failed: %s", exc)
            return None

    async def _run_review(self, incident: IncidentState, fix: FixResult) -> str | None:
        """Run CodeReviewAgent on the new PR and post review to GitHub."""
        if not fix.pr_number:
            return None
        owner, repo = settings.fix_target_repo.split("/", 1)
        try:
            result = await self._review_agent.run(
                f'{{"owner": "{owner}", "repo": "{repo}", '
                f'"pr_number": {fix.pr_number}, "post_to_github": true}}'
            )
            return result.answer
        except Exception as exc:
            logger.error("[IncidentLoop] CodeReviewAgent failed: %s", exc)
            return None

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
            incident_store.update(incident)
            await _notify_diagnosis(incident, diagnosis)
            return

        # High confidence → generate fix
        incident.status = IncidentStatus.FIXING
        incident_store.update(incident)
        await _notify_diagnosis(incident, diagnosis)

        logger.info(
            "[IncidentLoop] %s → confidence %.0f%% — running FixGenerationAgent",
            incident.id, diagnosis.confidence * 100,
        )

        # ── Fix Generation ────────────────────────────────────────────
        fix = await self._run_fix(incident)
        if fix is None:
            logger.error("[IncidentLoop] %s — fix generation failed, leaving in FIXING", incident.id)
            return

        incident.pr_url = fix.pr_url
        incident.pr_created_at = datetime.utcnow()
        incident.fix_attempted = fix.fix_description[:200]
        incident.status = IncidentStatus.REVIEWING
        incident_store.update(incident)

        # Store PR for idempotency — any future duplicate triage event will see this
        if fix.pr_url and incident.error_event.error_type:
            incident_store.set_pr_for_resource(incident.error_event.error_type, fix.pr_url)

        logger.info(
            "[IncidentLoop] %s — PR created: %s — running CodeReviewAgent",
            incident.id, fix.pr_url,
        )

        # ── Code Review ───────────────────────────────────────────────
        review_text = await self._run_review(incident, fix)
        if review_text:
            logger.info("[IncidentLoop] %s — code review posted to GitHub PR", incident.id)
        else:
            logger.warning("[IncidentLoop] %s — code review skipped or failed", incident.id)

        # ── Human Approval Gate ───────────────────────────────────────
        event = incident.error_event
        sev_str = str(event.severity).split(".")[-1] if event.severity else "P2"
        approval_req = await approval_service.request_approval(
            agent_name="FixGenerationAgent",
            action="merge_ai_fix_pr",
            parameters={
                "incident_id": incident.id,
                "pr_url": fix.pr_url,
                "pr_number": fix.pr_number,
                "branch": fix.branch,
                "confidence": f"{incident.confidence:.0%}",
            },
            risk_level=RiskLevel.HIGH,
            description=(
                f"AI-generated fix for [{sev_str}] {event.title} "
                f"(confidence {incident.confidence:.0%}, {incident.occurrences_24h} occurrences/24h). "
                f"PR: {fix.pr_url}"
            ),
        )

        incident.status = IncidentStatus.AWAITING_APPROVAL
        incident_store.update(incident)

        await _notify_fix_ready(incident, fix, approval_req.id)
        logger.info(
            "[IncidentLoop] %s — approval %s sent to Slack",
            incident.id, approval_req.id,
        )

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
