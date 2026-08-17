"""
Incident feed API — list, filter, and inspect incidents.

POST /incidents/trigger  — inject a test ErrorEvent directly into the pipeline
"""
import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from app.api.websocket_dashboard import broadcast
from app.core.config import settings
from app.models.events import ErrorEvent, EventSource, IncidentStatus
from app.services.aws import AWSService
from app.services.detection import classify_ecs_log
from app.services.event_queue import event_queue
from app.services.incident_store import incident_store
from app.services.pending_events import content_signature, pending_event_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/incidents", tags=["incidents"])


# ---------------------------------------------------------------------------
# Trigger body
# ---------------------------------------------------------------------------

class TriggerBody(BaseModel):
    error_type: str = "UNHANDLED_EXCEPTION"
    title: str = "Unhandled exception in production"
    description: str = "An unhandled exception was detected in production"
    service: str = "unknown"
    source: str = "application"
    log_group: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


@router.post("/trigger")
async def trigger_incident(body: TriggerBody) -> Dict[str, Any]:
    """
    Inject an ErrorEvent into the incident pipeline for testing.

    All fields are required — override them to simulate any incident scenario.
    """
    extra_meta = dict(body.metadata)
    if body.log_group:
        extra_meta["log_group"] = body.log_group

    event = ErrorEvent(
        source=EventSource(body.source),
        error_type=body.error_type,
        title=body.title,
        description=body.description,
        service=body.service,
        metadata=extra_meta,
    )
    await event_queue.enqueue(event)
    return {"status": "queued", "event_id": event.id, "title": event.title}


def _scan_ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


_scan_logger = logging.getLogger("scan")

async def _scan_log(message: str, level: str = "info") -> None:
    _scan_logger.info("[scan] %s", message)
    await broadcast({"type": "scan_progress", "ts": _scan_ts(), "level": level, "message": message})


def _has_active_incident(event: ErrorEvent) -> bool:
    """True if an incident already exists (any status) for this crash signature.

    A scan should skip re-queuing a signature that's either already resolved (a
    fix was merged for it) or still actively in flight (awaiting approval, being
    fixed, sitting in fix_failed) -- there's no value in spawning a second
    incident for either case. Once a human explicitly deletes the incident
    (wanting a fresh look -- e.g. an attempt that never actually merged), it
    drops out of incident_store and this returns False again, so the very next
    scan treats a real recurrence as new instead of silently absorbing it into
    pending_events' occurrence counter with no visible incident anywhere.
    """
    sig = content_signature(event)
    return any(content_signature(i.error_event) == sig for i in incident_store.list_all())


async def _run_scan(days: int) -> Dict[str, Any]:
    aws = AWSService()
    raw: str = getattr(settings, "ecs_log_groups", "")
    log_groups = [g.strip() for g in raw.split(",") if g.strip()] if raw else []
    region = getattr(settings, "ecs_log_groups_region", "") or None

    pending_event_store.reset_dismissed()
    _SCAN_CAP = 200
    minutes = days * 24 * 60
    await _scan_log(
        f"Scan started — checking {len(log_groups)} log group(s) over last {days} days "
        f"(cap: {_SCAN_CAP} events)"
    )

    queued = []
    errors = []
    capped = False
    for log_group in log_groups:
        if len(queued) >= _SCAN_CAP:
            capped = True
            break
        service = log_group.rstrip("/").split("/")[-1]
        await _scan_log(f"Scanning {log_group} ...")
        try:
            matches = aws.get_error_logs(log_group, minutes=minutes, region=region)
            await _scan_log(f"  {len(matches)} raw log entries fetched")

            seen: set[tuple[str, str, str]] = set()
            for log in matches:
                if len(queued) >= _SCAN_CAP:
                    capped = True
                    break
                msg = log["message"]
                stream = log["stream"]
                timestamp = str(log["timestamp"])
                sig = (stream, timestamp, msg[:600])
                if sig in seen:
                    continue
                seen.add(sig)

                classified = classify_ecs_log(msg)
                if classified is None:
                    continue
                error_type, category = classified

                stream_parts = stream.rsplit("/", 1)
                task_id = stream_parts[-1] if len(stream_parts) > 1 else stream

                event = ErrorEvent(
                    source=EventSource.CLOUDWATCH,
                    error_type=error_type,
                    task_id=task_id,
                    title=f"{error_type} in {service}",
                    description=msg[:3000],
                    service=service,
                    resource_id=log_group,
                    category=category,
                    metadata={
                        "log_group": log_group,
                        "task_id": task_id,
                        "timestamp": log["timestamp"],
                    },
                )

                if _has_active_incident(event):
                    await _scan_log(
                        f"  ✓ already tracked (incident exists): [{error_type}] {msg[:80].strip()}",
                        level="info",
                    )
                    continue

                pe, is_new = pending_event_store.add(event)
                if pe is None:
                    continue
                msg_type = "pending_event_added" if is_new else "pending_event_updated"
                await broadcast({"type": msg_type, "event": pending_event_store.serialize(pe)})
                if is_new:
                    queued.append({"id": pe.id, "title": event.title, "service": service})
                    if category == "crash":
                        # Crashes appear in the Crashes tab AND auto-enqueue to the pipeline
                        await event_queue.enqueue(event)
                        await broadcast({"type": "crash_auto_queued", "title": event.title})
                        await _scan_log(f"  → crash auto-queued: {msg[:80].strip()}", level="event")
                    else:
                        await _scan_log(f"  → pending approval: [{error_type}] {msg[:80].strip()}", level="event")
                else:
                    await _scan_log(
                        f"  ↻ duplicate ({pe.occurrences}× seen): [{error_type}] {msg[:80].strip()}",
                        level="info",
                    )

        except Exception as exc:
            errors.append({"log_group": log_group, "error": str(exc)})
            await _scan_log(f"  Error scanning {log_group}: {exc}", level="error")

    if capped:
        await _scan_log(f"Reached {_SCAN_CAP}-event cap — stopping scan early", level="info")

    summary = f"Scan complete — {len(queued)} event(s) awaiting approval"
    if capped:
        summary += f" (capped at {_SCAN_CAP})"
    if not queued:
        summary = "Scan complete — no new errors detected"
    await _scan_log(summary, level="done")

    return {
        "events_found": len(queued),
        "events": queued,
        **({"scan_errors": errors} if errors else {}),
    }


@router.post("/scan")
async def scan_last_7_days() -> Dict[str, Any]:
    """On-demand scan of configured ECS log groups over the last 7 days."""
    return await _run_scan(7)


@router.post("/scan/14days")
async def scan_last_14_days() -> Dict[str, Any]:
    """On-demand scan of configured ECS log groups over the last 14 days."""
    return await _run_scan(14)


@router.post("/scan/6weeks")
async def scan_last_6_weeks() -> Dict[str, Any]:
    """On-demand scan of configured ECS log groups over the last 6 weeks."""
    return await _run_scan(42)


@router.post("/scan/crashes")
async def scan_crashes_4_weeks() -> Dict[str, Any]:
    """Scan the last 4 weeks of ECS logs, returning APP_CRASHED events only."""
    aws = AWSService()
    raw: str = getattr(settings, "ecs_log_groups", "")
    log_groups = [g.strip() for g in raw.split(",") if g.strip()] if raw else []
    region = getattr(settings, "ecs_log_groups_region", "") or None

    _SCAN_CAP = 200
    _FOUR_WEEKS = 40_320  # 28 * 24 * 60 minutes
    await _scan_log(
        f"Crash scan started — checking {len(log_groups)} log group(s) over last 4 weeks "
        f"(crashes only, cap: {_SCAN_CAP})"
    )

    queued = []
    errors = []
    capped = False
    for log_group in log_groups:
        if len(queued) >= _SCAN_CAP:
            capped = True
            break
        service = log_group.rstrip("/").split("/")[-1]
        await _scan_log(f"Scanning {log_group} ...")
        try:
            # Scoped to crash-shaped lines only (classify_ecs_log's ONLY crash
            # trigger is the literal substring "app crashed") -- not the generic
            # multi-category default. Sharing that default pattern here would
            # spend the day-chunked fetch's shared event budget on generic
            # Errors/DeprecationWarnings this scan is about to throw away
            # anyway, starving out an older, rarer real crash line. Confirmed
            # in production: a real crash from ~36 hours back never appeared in
            # a 4-week "crashes only" scan because a noisier recent day
            # consumed the whole budget before the chunk loop reached that far.
            matches = aws.get_error_logs(
                log_group, minutes=_FOUR_WEEKS, region=region, filter_pattern='"app crashed"',
            )
            await _scan_log(f"  {len(matches)} raw log entries fetched")

            seen: set[tuple[str, str, str]] = set()
            for log in matches:
                if len(queued) >= _SCAN_CAP:
                    capped = True
                    break
                msg = log["message"]
                stream = log["stream"]
                timestamp = str(log["timestamp"])
                sig = (stream, timestamp, msg[:600])
                if sig in seen:
                    continue
                seen.add(sig)

                classified = classify_ecs_log(msg)
                if classified is None:
                    continue
                error_type, category = classified

                # Crashes only
                if category != "crash":
                    continue

                stream_parts = stream.rsplit("/", 1)
                task_id = stream_parts[-1] if len(stream_parts) > 1 else stream

                event = ErrorEvent(
                    source=EventSource.CLOUDWATCH,
                    error_type=error_type,
                    task_id=task_id,
                    title=f"{error_type} in {service}",
                    description=msg[:3000],
                    service=service,
                    resource_id=log_group,
                    category=category,
                    metadata={
                        "log_group": log_group,
                        "task_id": task_id,
                        "timestamp": log["timestamp"],
                    },
                )

                if _has_active_incident(event):
                    await _scan_log(
                        f"  ✓ already tracked (incident exists): [{error_type}] {msg[:80].strip()}",
                        level="info",
                    )
                    continue

                pe, is_new = pending_event_store.add(event)
                if pe is None:
                    continue
                msg_type = "pending_event_added" if is_new else "pending_event_updated"
                await broadcast({"type": msg_type, "event": pending_event_store.serialize(pe)})
                if is_new:
                    queued.append({"id": pe.id, "title": event.title, "service": service})
                    await event_queue.enqueue(event)
                    await broadcast({"type": "crash_auto_queued", "title": event.title})
                    await _scan_log(f"  → crash auto-queued: {msg[:80].strip()}", level="event")
                else:
                    await _scan_log(
                        f"  ↻ duplicate ({pe.occurrences}× seen): [{error_type}] {msg[:80].strip()}",
                        level="info",
                    )

        except Exception as exc:
            errors.append({"log_group": log_group, "error": str(exc)})
            await _scan_log(f"  Error scanning {log_group}: {exc}", level="error")

    if capped:
        await _scan_log(f"Reached {_SCAN_CAP}-event cap — stopping scan early", level="info")

    summary = f"Crash scan complete — {len(queued)} crash(es) found"
    if capped:
        summary += f" (capped at {_SCAN_CAP})"
    if not queued:
        summary = "Crash scan complete — no new crashes detected"
    await _scan_log(summary, level="done")

    return {
        "events_found": len(queued),
        "events": queued,
        **({"scan_errors": errors} if errors else {}),
    }


@router.get("")
async def list_incidents() -> List[Dict[str, Any]]:
    """All incidents, newest first."""
    return [_serialize(i) for i in incident_store.list_all()]


@router.get("/active")
async def list_active_incidents() -> List[Dict[str, Any]]:
    """Active incidents (excludes resolved, noise, duplicate)."""
    return [_serialize(i) for i in incident_store.list_active()]


@router.get("/metrics")
async def get_metrics() -> Dict[str, Any]:
    """MTTD/MTTR, false positive rate, totals, dedup layer hit rates, and LLM costs."""
    from app.services.incident_loop import incident_loop
    from app.services.llm_gateway import llm_gateway
    metrics = incident_store.metrics()
    stats = incident_loop.dedup_stats
    total = sum(stats.values()) or 1
    metrics["pipeline_stats"] = {
        **stats,
        "sql_dedup_pct":   round(stats["sql_dedup"]  / total * 100, 1),
        "regression_pct":  round(stats["regression"] / total * 100, 1),
        "rag_hit_pct":     round(stats["rag_hit"]    / total * 100, 1),
        "cold_start_pct":  round(stats["cold_start"] / total * 100, 1),
    }
    metrics.update(llm_gateway.costs_today())
    return metrics


@router.delete("")
async def clear_incidents() -> Dict[str, Any]:
    """Delete all non-resolved incidents from the store (memory + disk). Resolved incidents are preserved."""
    # Capture before clear() — its return value is just a count, and once an
    # incident is gone we lose the error_event needed to forget its pending-events
    # dedup fingerprint (see delete_incident's comment for why that matters).
    to_forget = [i for i in incident_store.list_all() if i.status != IncidentStatus.RESOLVED]
    count = incident_store.clear()
    for incident in to_forget:
        pending_event_store.forget_matching(incident.error_event)
    remaining = [_serialize(i) for i in incident_store.list_all()]
    await broadcast({"type": "incidents_cleared", "incidents": remaining, "deleted": count})
    return {"deleted": count}


@router.delete("/events")
async def clear_events() -> Dict[str, Any]:
    """Clear all pending events from the event queue."""
    removed = pending_event_store.clear()
    await broadcast({"type": "pending_events_cleared"})
    return {"deleted": len(removed)}


class RestartBody(BaseModel):
    notes: Optional[str] = None


@router.post("/{incident_id}/restart")
async def restart_incident(incident_id: str, body: RestartBody = RestartBody()) -> Dict[str, Any]:
    """
    Restart the pipeline for a stuck or failed incident.

    Resets the incident status to OPEN, clears all pipeline fields,
    and re-queues the original error event so the full pipeline runs again.
    Optional notes are stored on the incident and injected into the fix prompt.
    """
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    incident.status = IncidentStatus.OPEN
    incident.triage_decision = None
    incident.diagnosis = None
    incident.confidence = None
    incident.pr_url = None
    incident.pr_number = None
    incident.pr_branch = None
    incident.pr_files_changed = []
    incident.pr_test_added = False
    incident.fix_description = None
    incident.fix_attempted = None
    incident.human_decision = None
    incident.outcome = None
    incident.resolved_at = None
    incident.triage_completed_at = None
    incident.diagnosis_completed_at = None
    incident.pr_created_at = None
    incident.human_notes = body.notes or None
    incident_store.update(incident)

    # Mark as restarted so the staleness gate doesn't drop it
    incident.error_event.metadata["restarted"] = True
    incident.error_event.detected_at = datetime.now(timezone.utc)
    await event_queue.enqueue(incident.error_event)
    return {"status": "restarted", "incident_id": incident_id}


@router.post("/{incident_id}/approve-fix")
async def approve_fix(incident_id: str) -> Dict[str, Any]:
    """
    Approve the pending diff for an incident in AWAITING_FIX_APPROVAL state.
    Commits the fix to GitHub and opens a PR.
    """
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if not incident.pending_fix_old:
        raise HTTPException(status_code=400, detail="No pending fix to approve")

    from app.agents.fix_generation import FixGenerationAgent
    from app.services.incident_loop import _apply_dod_gate, incident_loop

    agent = FixGenerationAgent()
    fix, steps = await agent.commit_approved_fix(incident)

    if not fix.pr_url:
        raise HTTPException(status_code=500, detail=f"Commit failed: {fix.fix_description}")

    incident.pr_url = fix.pr_url
    incident.pr_number = fix.pr_number
    incident.pr_branch = fix.branch
    incident.pr_files_changed = fix.files_changed
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

    # Clear pending diff before DoD gate
    incident.pending_fix_old = None
    incident.pending_fix_new = None
    incident.pending_fix_file = None
    incident.pending_fix_branch = None
    incident.pending_fix_issue_url = None
    incident.pending_fix_issue_number = None
    incident.pending_fix_function = None
    incident.pending_fix_critique = None

    if not await _apply_dod_gate(incident):
        return {
            "status": "verification_failed",
            "incident_id": incident_id,
            "failed_checks": incident.dod_failed_checks,
        }

    incident.status = IncidentStatus.REVIEWING
    incident_store.update(incident)

    asyncio.ensure_future(incident_loop._run_post_fix(incident, fix))

    return {"status": "approved", "pr_url": fix.pr_url, "pr_number": fix.pr_number}


@router.post("/{incident_id}/reject-fix")
async def reject_fix(incident_id: str, body: RestartBody = RestartBody()) -> Dict[str, Any]:
    """
    Reject the pending diff and optionally provide notes for a better fix.
    Clears the pending diff — use the Restart endpoint to re-run with notes.
    """
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    incident.pending_fix_old = None
    incident.pending_fix_new = None
    incident.pending_fix_file = None
    incident.pending_fix_branch = None
    incident.pending_fix_issue_url = None
    incident.pending_fix_issue_number = None
    incident.pending_fix_function = None
    incident.pending_fix_critique = None
    incident.human_notes = body.notes or incident.human_notes
    incident.status = IncidentStatus.FIXING
    incident_store.update(incident)
    return {"status": "rejected", "incident_id": incident_id}


class RefixBody(BaseModel):
    notes: Optional[str] = None


@router.post("/{incident_id}/refix")
async def approve_refix(incident_id: str, background_tasks: BackgroundTasks, body: RefixBody = RefixBody()) -> Dict[str, Any]:
    """
    Approve re-running fix generation with the code review feedback as human_notes.
    Optional notes are prepended to the existing human_notes so the fix agent sees them.
    Only valid when the incident is in AWAITING_REFIX_APPROVAL status.
    """
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.status != IncidentStatus.AWAITING_REFIX_APPROVAL:
        raise HTTPException(
            status_code=400,
            detail=f"Incident is not awaiting re-fix approval (status={incident.status})",
        )
    if body.notes:
        existing = incident.human_notes or ""
        incident.human_notes = f"HUMAN INSTRUCTION: {body.notes.strip()}\n\n{existing}".strip()
        incident_store.update(incident)
    from app.services.incident_loop import incident_loop
    background_tasks.add_task(incident_loop.refix_from_review, incident_id)
    return {"status": "refix_queued", "incident_id": incident_id}


@router.post("/{incident_id}/reject-refix")
async def reject_refix(incident_id: str) -> Dict[str, Any]:
    """
    Reject re-running the fix — mark the incident as rejected.
    Only valid when the incident is in AWAITING_REFIX_APPROVAL status.
    """
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    if incident.status != IncidentStatus.AWAITING_REFIX_APPROVAL:
        raise HTTPException(
            status_code=400,
            detail=f"Incident is not awaiting re-fix approval (status={incident.status})",
        )
    incident.status = IncidentStatus.REJECTED
    incident_store.update(incident)
    return {"status": "rejected", "incident_id": incident_id}


@router.post("/{incident_id}/resolve")
async def resolve_incident(incident_id: str) -> Dict[str, Any]:
    """Manually mark an incident as resolved."""
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident.status = IncidentStatus.RESOLVED
    incident.human_decision = "approved"
    incident.outcome = "manually_resolved"
    incident.resolved_at = datetime.now(timezone.utc)
    incident_store.update(incident)
    return {"status": "resolved", "incident_id": incident_id}


@router.post("/{incident_id}/mark-merged")
async def mark_merged(incident_id: str) -> Dict[str, Any]:
    """Mark a PR as merged — records resolved_at now and sets outcome=fix_merged for MTTR."""
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident.status = IncidentStatus.RESOLVED
    incident.outcome = "fix_merged"
    incident.human_decision = "approved"
    incident.resolved_at = datetime.now(timezone.utc)
    incident_store.update(incident)
    return {"status": "merged", "incident_id": incident_id}


@router.post("/{incident_id}/unresolve")
async def unresolve_incident(incident_id: str) -> Dict[str, Any]:
    """Move a resolved incident back to awaiting_approval (PR still exists)."""
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident.status = IncidentStatus.AWAITING_APPROVAL
    incident.human_decision = None
    incident.outcome = None
    incident.resolved_at = None
    incident_store.update(incident)
    return {"status": "unresolved", "incident_id": incident_id}


@router.delete("/{incident_id}")
async def delete_incident(incident_id: str) -> Dict[str, Any]:
    """Permanently delete a single incident from the store.

    Failure annotations in agent_failures keep their incident_id pointer but are
    NOT removed — the dataset is append-only and survives incident deletion.
    """
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident_store.delete(incident_id)
    # Also forget the pending-events dedup fingerprint for this incident's error —
    # otherwise a real recurrence of the exact same crash gets silently absorbed as
    # "occurrences++" on a pending record with no visible incident, and a future
    # crash scan reports "no new crashes" even though this is still live.
    pending_event_store.forget_matching(incident.error_event)
    await broadcast({"type": "incident_deleted", "id": incident_id})
    return {"status": "deleted", "incident_id": incident_id}


@router.post("/{incident_id}/archive")
async def archive_incident(incident_id: str) -> Dict[str, Any]:
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident.archived = True
    incident_store.update(incident)
    return {"status": "archived", "incident_id": incident_id}


class WrongFixBody(BaseModel):
    notes: str


@router.post("/{incident_id}/mark-wrong-fix")
async def mark_wrong_fix(incident_id: str, body: WrongFixBody) -> Dict[str, Any]:
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    incident.wrong_fix = True
    incident.wrong_fix_notes = body.notes
    incident_store.update(incident)
    return {"status": "marked", "incident_id": incident_id}


@router.get("/{incident_id}")
async def get_incident(incident_id: str) -> Dict[str, Any]:
    incident = incident_store.get(incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return _serialize(incident)


def _serialize(incident) -> Dict[str, Any]:
    d = incident.model_dump()
    # Add computed fields
    d["mttr_seconds"] = incident.mttr_seconds
    d["age_seconds"] = incident.age_seconds
    return d
