"""
Incident feed API — list, filter, and inspect incidents.

POST /incidents/trigger  — inject a test ErrorEvent directly into the pipeline
"""
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.websocket_dashboard import broadcast
from app.core.config import settings
from app.models.events import ErrorEvent, EventSource
from app.services.aws import AWSService
from app.services.event_queue import event_queue
from app.services.incident_store import incident_store

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


import logging as _logging
_scan_logger = _logging.getLogger("scan")

async def _scan_log(message: str, level: str = "info") -> None:
    _scan_logger.info("[scan] %s", message)
    await broadcast({"type": "scan_progress", "ts": _scan_ts(), "level": level, "message": message})


@router.post("/scan")
async def scan_last_24h() -> Dict[str, Any]:
    """
    Scan ECS log groups for errors in the last 24 hours and feed each into
    the full triage → diagnosis → fix pipeline.
    """
    aws = AWSService()
    raw: str = getattr(settings, "ecs_log_groups", "")
    log_groups = [g.strip() for g in raw.split(",") if g.strip()] if raw else []

    await _scan_log(f"Scan started — checking {len(log_groups)} log group(s)")

    queued = []
    errors = []
    for log_group in log_groups:
        service = log_group.rstrip("/").split("/")[-1]
        await _scan_log(f"Scanning {log_group} ...")
        try:
            matches = aws.get_error_logs(log_group, minutes=1440, limit=100)
            await _scan_log(f"  {len(matches)} raw log entries fetched")

            seen: set[str] = set()
            for log in matches:
                msg = log["message"]
                # Normalize variable parts (byte offsets, numbers) for dedup
                normalized = re.sub(r'\b\d+\b', 'N', msg[:120]).strip()
                sig = normalized[:80]
                if sig in seen:
                    continue
                seen.add(sig)

                exc_match = re.search(
                    r'\b([A-Z][a-zA-Z0-9]*(?:Error|Exception|Fault|Warning))\b', msg
                )
                error_type = exc_match.group(1).upper() if exc_match else "ECS_ERROR"

                stream_parts = log["stream"].rsplit("/", 1)
                task_id = stream_parts[-1] if len(stream_parts) > 1 else log["stream"]

                event = ErrorEvent(
                    source=EventSource.CLOUDWATCH,
                    error_type=error_type,
                    task_id=task_id,
                    title=f"{error_type} in {service}",
                    description=msg[:600],
                    service=service,
                    resource_id=log_group,
                    metadata={
                        "log_group": log_group,
                        "task_id": task_id,
                        "timestamp": log["timestamp"],
                    },
                )
                await event_queue.enqueue(event)
                queued.append({"id": event.id, "title": event.title, "service": service})
                await _scan_log(f"  → queued: [{error_type}] {msg[:80].strip()}", level="event")

                if len(seen) >= 10:
                    await _scan_log(f"  Reached 10-event cap for {log_group}")
                    break

        except Exception as exc:
            errors.append({"log_group": log_group, "error": str(exc)})
            await _scan_log(f"  Error scanning {log_group}: {exc}", level="error")

    summary = f"Scan complete — {len(queued)} event(s) queued into pipeline"
    if not queued:
        summary = "Scan complete — no new errors detected"
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
    """MTTD/MTTR, false positive rate, totals, and dedup layer hit rates."""
    from app.services.incident_loop import incident_loop
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
    return metrics


@router.delete("")
async def clear_incidents() -> Dict[str, Any]:
    """Delete all incidents from the store (memory + disk)."""
    count = incident_store.clear()
    return {"deleted": count}


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

    from app.models.events import IncidentStatus
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
    from app.models.events import IncidentStatus
    agent = FixGenerationAgent()
    fix, steps = await agent.commit_approved_fix(incident)

    if not fix.pr_url:
        raise HTTPException(status_code=500, detail=f"Commit failed: {fix.fix_description}")

    incident.pr_url = fix.pr_url
    incident.pr_number = fix.pr_number
    incident.pr_branch = fix.branch
    incident.pr_files_changed = fix.files_changed
    incident.fix_description = fix.fix_description
    incident.status = IncidentStatus.REVIEWING
    # Clear pending diff
    incident.pending_fix_old = None
    incident.pending_fix_new = None
    incident.pending_fix_file = None
    incident.pending_fix_branch = None
    incident.pending_fix_issue_url = None
    incident.pending_fix_issue_number = None
    incident.pending_fix_function = None
    incident.pending_fix_critique = None
    incident_store.update(incident)

    # Kick off code review in background
    from app.services.incident_loop import incident_loop
    import asyncio
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

    from app.models.events import IncidentStatus
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
