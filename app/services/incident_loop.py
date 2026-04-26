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
                  → DIAGNOSING → AWAITING_APPROVAL (low confidence — human approves or rejects)
                                                  → FIXING → REVIEWING → AWAITING_APPROVAL (fix gate)
                                                                        → RESOLVED / REJECTED
                              → FIXING → REVIEWING → AWAITING_APPROVAL (human gate)
                                                   → RESOLVED / REJECTED
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.agents.code_review import CodeReviewAgent
from app.agents.diagnosis import CONFIDENCE_THRESHOLD, DiagnosisAgent, DiagnosisResult
from app.agents.fix_generation import FixGenerationAgent, FixResult
from app.agents.triage import TriageAgent, TriageResult
from app.core.config import settings
from app.models.events import ErrorEvent, IncidentState, IncidentStatus, Severity
from app.services.alerting import Alert, alerting_service
from app.services.alerting import Severity as AlertSeverity
from app.services.approvals import RiskLevel, approval_service
from app.services.circuit_breaker import CircuitOpenError, circuit_breaker_registry
from app.services.dod_checker import dod_checker
from app.services.event_queue import event_queue
from app.services.incident_store import incident_store
from app.services.schema_validator import HandoffValidationError, handoff_validator

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


async def _notify_blast_radius_violation(incident: IncidentState, fix: FixResult) -> None:
    event = incident.error_event
    sev_str = str(event.severity).split(".")[-1] if event.severity else "P2"
    violations_text = "\n".join(f"  • {v}" for v in fix.blast_radius_violations)
    message = (
        f"*Service:* {event.service}  |  *Severity:* {sev_str}\n"
        f"*Root cause:* {incident.diagnosis}\n"
        f"*Violations:*\n{violations_text}\n\n"
        f"The AI fix was blocked before creating any branch or PR.\n"
        f"A human must review and apply this fix manually.\n"
        f"_Incident:_ {incident.id}"
    )
    await alerting_service.send_alert(Alert(
        severity=AlertSeverity.WARNING,
        title=f"[{sev_str}] AI fix blocked by blast radius limiter — manual fix required",
        message=message,
        source="BlastRadiusGuard",
        metadata={"incident_id": incident.id, "violations": fix.blast_radius_violations},
    ))


async def _notify_diagnosis(incident: IncidentState, result: DiagnosisResult, approval_id: str | None = None) -> None:
    event = incident.error_event
    sev = _p_to_alert_sev(str(event.severity).split(".")[-1] if event.severity else "P2")

    if result.escalate:
        title = f"[LOW CONFIDENCE] {event.title} — human review needed"
        if approval_id:
            approve_url = f"{settings.approval_base_url}/approvals/{approval_id}/approve"
            reject_url = f"{settings.approval_base_url}/approvals/{approval_id}/reject"
            approval_links = (
                f"\n<{approve_url}|✅ Approve — continue to fix generation>  |  "
                f"<{reject_url}|❌ Reject>\n_Approval ID: {approval_id}_"
            )
        else:
            approval_links = ""
        message = (
            f"*Confidence:* {result.confidence:.0%}  (threshold {CONFIDENCE_THRESHOLD:.0%})\n"
            f"*Root cause:* {result.root_cause}\n"
            f"*Evidence:* {', '.join(result.evidence[:2])}\n"
            f"*Reproduction:* {'✅ confirmed' if result.reproduction_confirmed else '❌ not confirmed'}\n"
            f"*Incident:* {incident.id} — awaiting human diagnosis"
            + approval_links
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
# Definition of Done gate
# ---------------------------------------------------------------------------

async def _apply_dod_gate(incident: IncidentState) -> bool:
    """
    Run all DoD checks before advancing an incident to REVIEWING.

    Returns True if all checks pass (caller should proceed to REVIEWING).
    Returns False if any check failed; the incident is left in VERIFICATION_FAILED
    and the caller should return without further processing.
    """
    results = await dod_checker.run_all(incident)
    failed = {
        name: evidence
        for name, (passed, evidence) in results.items()
        if not passed
    }
    if not failed:
        return True

    incident.status = IncidentStatus.VERIFICATION_FAILED
    incident.dod_failed_checks = failed
    incident_store.update(incident)

    failed_names = ", ".join(failed.keys())
    logger.warning(
        "[IncidentLoop] %s — DoD gate FAILED (%s), blocking REVIEWING: %s",
        incident.id, failed_names, failed,
    )
    return False


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
        try:
            from app.services.rag import RAGService
            self._rag: RAGService | None = RAGService()
        except Exception:
            self._rag = None
        self._dedup_stats: dict[str, int] = {
            "sql_dedup": 0,
            "regression": 0,
            "rag_hit": 0,
            "cold_start": 0,
        }

    @property
    def dedup_stats(self) -> dict[str, int]:
        return dict(self._dedup_stats)

    # ------------------------------------------------------------------ #
    # Step 1 — Triage
    # ------------------------------------------------------------------ #

    async def _run_triage(self, event: ErrorEvent) -> TriageResult:
        try:
            result = await self._triage.triage(event)
            return handoff_validator.validate_triage(result)
        except HandoffValidationError as exc:
            logger.error("[IncidentLoop] TriageResult schema invalid: %s", exc)
            return TriageResult(
                decision="real", severity="P2", blast_radius="unknown",
                occurrences_24h=0, duplicate_pr=None,
                reasoning=f"Triage schema validation failed ({exc}) — defaulting to real/P2",
            )
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
        cb = circuit_breaker_registry.get_or_create(
            "github_api", failure_threshold=3, timeout_seconds=120.0
        )
        try:
            fix, steps = await cb.call(self._fix_agent.fix_with_steps(incident))
            if not fix.pr_url and not fix.blast_radius_violation:
                logger.error(
                    "[IncidentLoop] FixGenerationAgent failed for %s — steps:\n%s",
                    incident.id, "\n".join(steps),
                )
            return fix
        except CircuitOpenError:
            logger.warning("[IncidentLoop] GitHub circuit breaker OPEN — skipping fix for %s", incident.id)
            return None
        except Exception as exc:
            logger.error("[IncidentLoop] FixGenerationAgent raised exception for %s: %s", incident.id, exc)
            return None

    async def _run_review(self, incident: IncidentState, fix: FixResult) -> str | None:
        """Run CodeReviewAgent on the new PR and post review to GitHub."""
        if not fix.pr_number:
            return None
        try:
            handoff_validator.validate_fix_for_review(fix)
        except HandoffValidationError as exc:
            logger.error("[IncidentLoop] FixResult schema invalid for review: %s", exc)
            return None
        owner, repo = settings.fix_target_repo.split("/", 1)
        cb = circuit_breaker_registry.get_or_create(
            "github_api", failure_threshold=3, timeout_seconds=120.0
        )
        try:
            result = await cb.call(self._review_agent.run(
                f'{{"owner": "{owner}", "repo": "{repo}", '
                f'"pr_number": {fix.pr_number}, "post_to_github": true}}'
            ))
            return result.answer
        except CircuitOpenError:
            logger.warning("[IncidentLoop] GitHub circuit breaker OPEN — skipping review for %s", incident.id)
            return None
        except Exception as exc:
            logger.error("[IncidentLoop] CodeReviewAgent failed: %s", exc)
            return None

    async def _index_to_rag(self, incident: IncidentState) -> None:
        if self._rag is None:
            return
        try:
            await self._rag.index_incident(incident)
        except Exception as exc:
            logger.debug("[IncidentLoop] RAG index skipped for %s: %s", incident.id, exc)

    async def _run_diagnosis(self, incident: IncidentState, prior_context: str | None = None) -> DiagnosisResult:
        try:
            result = await self._diagnosis.diagnose(incident, prior_context=prior_context)
            return handoff_validator.validate_diagnosis(result)
        except HandoffValidationError as exc:
            logger.error("[IncidentLoop] DiagnosisResult schema invalid: %s", exc)
            return DiagnosisResult(
                root_cause=f"Diagnosis schema validation failed ({exc}) — manual review required",
                confidence=0.0,
                escalate=True,
            )
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
        # ── Staleness gate ────────────────────────────────────────────
        # Only process events detected within the last 30 minutes.
        # Stale events (replayed, delayed, or from before the server started)
        # are dropped silently to avoid acting on outdated data.
        detected = event.detected_at if event.detected_at.tzinfo else event.detected_at.replace(tzinfo=timezone.utc)
        age_minutes = (datetime.now(timezone.utc) - detected).total_seconds() / 60
        if age_minutes > 30:
            logger.info(
                "[IncidentLoop] Dropping stale event %s (%.0fm old) — outside 30-minute window",
                event.id, age_minutes,
            )
            return

        # ── Layer 1: open PR dedup gate ───────────────────────────────
        # Drop if an open PR already exists for same error_type + service + description.
        if event.error_type and event.service:
            existing_pr = incident_store.get_open_pr_for_error(
                event.error_type, event.service, event.description
            )
            if existing_pr:
                logger.info(
                    "[IncidentLoop] Dropping %s (%s / %s) — open PR already exists: %s",
                    event.id, event.error_type, event.service, existing_pr,
                )
                self._dedup_stats["sql_dedup"] += 1
                return

        # ── Layer 2: regression check (SQL) ──────────────────────────
        # If this exact error_type + service was previously resolved, surface
        # the past root cause and fix as context for DiagnosisAgent.
        prior_context: str | None = None
        if event.error_type and event.service:
            past = incident_store.get_resolved_for_error(event.error_type, event.service)
            if past:
                prior_context = (
                    f"REGRESSION: This error was previously resolved "
                    f"(incident {past.id}, resolved {past.resolved_at}).\n"
                    f"Past root cause: {past.diagnosis}\n"
                    f"Past fix: {past.fix_description or '(none recorded)'}\n"
                    f"Past PR: {past.pr_url or '(none)'}"
                )
                self._dedup_stats["regression"] += 1
                logger.info(
                    "[IncidentLoop] Regression detected for %s/%s — prior: %s",
                    event.error_type, event.service, past.id,
                )

        # ── Layer 3: RAG semantic search ──────────────────────────────
        # Only fires when Layer 2 found nothing. Costs one embedding API call.
        # Surfaces semantically similar past incidents (score ≥ 0.80) across
        # all services, not just exact error_type matches.
        if prior_context is None and self._rag is not None:
            try:
                query = f"{event.title} {event.description[:200]}"
                similar = await self._rag.search_incidents(query, min_score=0.80)
                if similar:
                    lines = ["Semantically similar past incidents (RAG, score ≥ 0.80):"]
                    for s in similar:
                        lines.append(
                            f"  [{s['score']:.2f}] {s['text'][:200]}\n"
                            f"    PR: {s['pr_url'] or 'none'} | Outcome: {s['status']}"
                        )
                    prior_context = "\n".join(lines)
                    self._dedup_stats["rag_hit"] += 1
                    logger.info(
                        "[IncidentLoop] RAG found %d similar incident(s) for %s",
                        len(similar), event.id,
                    )
            except Exception as exc:
                logger.debug("[IncidentLoop] RAG search skipped: %s", exc)

        if prior_context is None:
            self._dedup_stats["cold_start"] += 1

        # ── Triage ────────────────────────────────────────────────────
        incident = incident_store.create(event)
        incident.status = IncidentStatus.TRIAGING
        incident_store.update(incident)
        logger.info("[IncidentLoop] Triaging %s — %s", incident.id, event.title)

        # Link all agent runs in this pipeline task to the incident
        from app.agents.base import incident_id_ctx
        incident_id_ctx.set(incident.id)

        triage = await self._run_triage(event)

        incident.triage_decision = triage.decision
        incident.triage_reasoning = triage.reasoning
        incident.blast_radius = triage.blast_radius
        incident.occurrences_24h = triage.occurrences_24h
        incident.triage_completed_at = datetime.now(timezone.utc)

        if triage.decision == "duplicate":
            incident.status = IncidentStatus.DUPLICATE
            incident.pr_url = triage.duplicate_pr
            incident_store.update(incident)
            await _notify_triage(incident.id, event, triage)
            try:
                from app.services.golden_dataset_builder import golden_dataset_builder
                golden_dataset_builder.capture(incident)
            except Exception:
                pass
            await self._index_to_rag(incident)
            return

        if triage.decision == "noise":
            incident.status = IncidentStatus.NOISE
            incident_store.update(incident)
            await _notify_triage(incident.id, event, triage)
            try:
                from app.services.golden_dataset_builder import golden_dataset_builder
                golden_dataset_builder.capture(incident)
            except Exception:
                pass
            await self._index_to_rag(incident)
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
        diagnosis = await self._run_diagnosis(incident, prior_context=prior_context)

        incident.diagnosis = diagnosis.root_cause
        incident.confidence = diagnosis.confidence
        incident.reproduction_confirmed = diagnosis.reproduction_confirmed
        incident.diagnosis_completed_at = datetime.now(timezone.utc)

        if diagnosis.escalate:
            event = incident.error_event
            sev_str = str(event.severity).split(".")[-1] if event.severity else "P2"
            approval_req = await approval_service.request_approval(
                agent_name="DiagnosisAgent",
                action="approve_diagnosis_escalation",
                parameters={"incident_id": incident.id},
                risk_level=RiskLevel.HIGH,
                description=(
                    f"Low-confidence diagnosis ({diagnosis.confidence:.0%}) for "
                    f"[{sev_str}] {event.title}. Approve to proceed to fix generation."
                ),
            )
            incident.approval_id = approval_req.id
            incident.status = IncidentStatus.AWAITING_APPROVAL
            logger.warning(
                "[IncidentLoop] %s → low confidence (%.0f%%) — escalating to human (approval %s)",
                incident.id, diagnosis.confidence * 100, approval_req.id,
            )
            incident_store.update(incident)
            await _notify_diagnosis(incident, diagnosis, approval_id=approval_req.id)
            try:
                from app.services.golden_dataset_builder import golden_dataset_builder
                golden_dataset_builder.capture(incident)
            except Exception:
                pass
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
        # Skip for demo events — we want to show agent activity without
        # creating real GitHub branches and PRs every time.
        if incident.error_event.metadata.get("demo"):
            incident.status = IncidentStatus.AWAITING_APPROVAL
            incident.fix_attempted = "[Demo mode — fix generation skipped]"
            incident_store.update(incident)
            logger.info("[IncidentLoop] %s — demo event, skipping fix + PR creation", incident.id)
            return

        fix = await self._run_fix(incident)
        if fix is None:
            logger.error("[IncidentLoop] %s — fix generation failed, leaving in FIXING", incident.id)
            return

        # Blast radius gate — block the PR before any GitHub writes
        if fix.blast_radius_violation:
            incident.fix_attempted = fix.fix_description[:200]
            incident.status = IncidentStatus.AWAITING_APPROVAL
            incident_store.update(incident)
            await _notify_blast_radius_violation(incident, fix)
            logger.warning(
                "[IncidentLoop] %s — blast radius violation, escalating to human: %s",
                incident.id, fix.fix_description,
            )
            return

        # Pending approval gate — diff generated, waiting for human to approve before commit
        if incident.pending_fix_old and not fix.pr_url:
            incident.fix_attempted = fix.fix_description[:200]
            incident.status = IncidentStatus.AWAITING_FIX_APPROVAL
            incident_store.update(incident)
            logger.info("[IncidentLoop] %s — diff ready, awaiting human approval", incident.id)
            return

        if not fix.pr_url and not fix.pr_number:
            incident.fix_attempted = fix.fix_description[:200]
            incident_store.update(incident)
            logger.error("[IncidentLoop] %s — fix generation failed: %s", incident.id, fix.fix_description)
            return

        incident.pr_url = fix.pr_url
        incident.pr_number = fix.pr_number
        incident.pr_branch = fix.branch
        incident.pr_files_changed = fix.files_changed
        incident.pr_test_added = fix.test_added
        incident.fix_attempted = fix.fix_description[:200]
        incident.fix_description = fix.fix_description
        incident.issue_url = fix.issue_url

        # Register PR for dedup before DoD gate so monitor_pr_map check can see it
        if fix.pr_url and incident.error_event.error_type:
            key = (
                f"{incident.error_event.error_type}"
                f":{incident.error_event.service}"
                f":{incident.error_event.description[:100]}"
            )
            incident_store.set_pr_for_resource(key, fix.pr_url)
            if incident.monitor_id:
                incident_store.set_pr_for_resource(incident.monitor_id, fix.pr_url)

        if not await _apply_dod_gate(incident):
            return

        incident.status = IncidentStatus.REVIEWING
        incident_store.update(incident)

        logger.info("[IncidentLoop] %s — PR created: %s — running CodeReviewAgent", incident.id, fix.pr_url)
        await self._run_post_fix(incident, fix)

    async def _run_post_fix(self, incident: IncidentState, fix) -> None:
        """Run code review + approval gate after a fix PR has been created (shared by pipeline and approve-fix endpoint)."""
        incident.pr_created_at = datetime.now(timezone.utc)
        incident_store.update(incident)

        review_text = await self._run_review(incident, fix)
        if review_text:
            incident.review_posted = True
            incident_store.update(incident)

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
        incident.approval_id = approval_req.id
        incident.status = IncidentStatus.AWAITING_APPROVAL
        incident_store.update(incident)
        await _notify_fix_ready(incident, fix, approval_req.id)

    async def resume_fix(self, incident_id: str) -> None:
        """Resume the pipeline from fix generation after a human approves a low-confidence escalation."""
        incident = incident_store.get(incident_id)
        if incident is None:
            logger.error("[IncidentLoop] resume_fix: incident %s not found", incident_id)
            return

        incident.status = IncidentStatus.FIXING
        incident_store.update(incident)
        logger.info(
            "[IncidentLoop] %s — human approved diagnosis, resuming fix generation",
            incident_id,
        )

        if incident.error_event.metadata.get("demo"):
            incident.status = IncidentStatus.AWAITING_APPROVAL
            incident.fix_attempted = "[Demo mode — fix generation skipped]"
            incident_store.update(incident)
            return

        fix = await self._run_fix(incident)
        if fix is None:
            logger.error("[IncidentLoop] %s — fix generation failed after diagnosis approval", incident_id)
            return

        if fix.blast_radius_violation:
            incident.fix_attempted = fix.fix_description[:200]
            incident.status = IncidentStatus.AWAITING_APPROVAL
            incident_store.update(incident)
            await _notify_blast_radius_violation(incident, fix)
            logger.warning(
                "[IncidentLoop] %s — blast radius violation after resume: %s",
                incident_id, fix.fix_description,
            )
            return

        if incident.pending_fix_old and not fix.pr_url:
            incident.fix_attempted = fix.fix_description[:200]
            incident.status = IncidentStatus.AWAITING_FIX_APPROVAL
            incident_store.update(incident)
            logger.info("[IncidentLoop] %s — diff ready after resume, awaiting human approval", incident_id)
            return

        if not fix.pr_url and not fix.pr_number:
            incident.fix_attempted = fix.fix_description[:200]
            incident_store.update(incident)
            logger.error("[IncidentLoop] %s — fix generation produced no PR after resume: %s", incident_id, fix.fix_description)
            return

        incident.pr_url = fix.pr_url
        incident.pr_number = fix.pr_number
        incident.pr_branch = fix.branch
        incident.pr_files_changed = fix.files_changed
        incident.pr_test_added = fix.test_added
        incident.pr_created_at = datetime.now(timezone.utc)
        incident.fix_attempted = fix.fix_description[:200]
        incident.fix_description = fix.fix_description
        incident.issue_url = fix.issue_url

        # Register PR for dedup before DoD gate so monitor_pr_map check can see it
        if fix.pr_url and incident.error_event.error_type:
            key = (
                f"{incident.error_event.error_type}"
                f":{incident.error_event.service}"
                f":{incident.error_event.description[:100]}"
            )
            incident_store.set_pr_for_resource(key, fix.pr_url)
            if incident.monitor_id:
                incident_store.set_pr_for_resource(incident.monitor_id, fix.pr_url)

        if not await _apply_dod_gate(incident):
            return

        incident.status = IncidentStatus.REVIEWING
        incident_store.update(incident)

        logger.info("[IncidentLoop] %s — PR created: %s — running CodeReviewAgent", incident_id, fix.pr_url)

        review_text = await self._run_review(incident, fix)
        if review_text:
            incident.review_posted = True
            incident_store.update(incident)

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

        incident.approval_id = approval_req.id
        incident.status = IncidentStatus.AWAITING_APPROVAL
        incident_store.update(incident)

        await _notify_fix_ready(incident, fix, approval_req.id)
        logger.info("[IncidentLoop] %s — approval %s sent to Slack", incident_id, approval_req.id)

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
