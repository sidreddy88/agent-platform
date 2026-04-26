"""
GET  /agents/status  — current agent health snapshot.
POST /agents/demo    — inject the most recent ECS/CloudWatch error into the pipeline.
"""
from datetime import datetime

from fastapi import APIRouter

from app.models.events import ErrorEvent, EventSource, IncidentStatus
from app.services.agent_tracker import agent_tracker
from app.services.event_queue import event_queue
from app.services.incident_store import incident_store

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/status")
async def get_agent_status():
    """Return active runs, recent errors, and per-agent stats for today."""
    return agent_tracker.snapshot()


def _utc_iso(dt) -> str | None:
    """Serialize a naive UTC datetime to ISO 8601 with Z suffix."""
    if dt is None:
        return None
    s = dt.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


@router.get("/prs")
async def get_agent_prs():
    """Return one entry per unique PR number, newest first."""
    skip = {IncidentStatus.DUPLICATE, IncidentStatus.NOISE,
            IncidentStatus.RESOLVED, IncidentStatus.REJECTED}

    # Collect all qualifying incidents, then deduplicate by pr_number
    # keeping only the most recent incident per PR.
    seen_prs: dict[int, dict] = {}
    for incident in incident_store.list_all():
        if not incident.pr_number:
            continue
        if incident.status in skip:
            continue
        if incident.pr_number in seen_prs:
            continue  # list_all() is newest-first, so first seen wins
        event = incident.error_event
        seen_prs[incident.pr_number] = {
            "incident_id": incident.id,
            "pr_url": incident.pr_url,
            "pr_number": incident.pr_number,
            "title": event.title,
            "service": event.service,
            "severity": str(event.severity).split(".")[-1] if event.severity else None,
            "status": incident.status.value,
            "confidence": incident.confidence,
            "diagnosis": incident.diagnosis,
            "human_decision": incident.human_decision,
            "review_posted": incident.review_posted,
            "pr_branch": incident.pr_branch,
            "pr_files_changed": incident.pr_files_changed,
            "pr_test_added": incident.pr_test_added,
            "occurrences_24h": incident.occurrences_24h,
            "pr_created_at": _utc_iso(incident.pr_created_at),
            "resolved_at": _utc_iso(incident.resolved_at),
            "mttr_seconds": incident.mttr_seconds,
            "agent_runs": agent_tracker.get_runs_for_incident(incident.id),
        }

    prs = list(seen_prs.values())
    return {"prs": prs, "total": len(prs)}


@router.post("/demo")
async def run_demo():
    """
    Pick the most recent CloudWatch/ECS incident and re-inject its error
    event into the pipeline as a fresh event so you can watch all agents run.
    Falls back to a synthetic ECS error if no prior incidents exist.
    """
    # Find the most recent cloudwatch incident
    source_event: ErrorEvent | None = None
    origin = "synthetic"

    for incident in incident_store.list_all():
        ev = incident.error_event
        if ev.source == EventSource.CLOUDWATCH:
            source_event = ev
            origin = "existing"
            break

    if source_event:
        # Clone with a fresh id and timestamp so it flows as a new incident
        event = ErrorEvent(
            source=source_event.source,
            error_type=source_event.error_type,
            task_id=source_event.task_id,
            title=source_event.title,
            description=source_event.description,
            service=source_event.service,
            resource_id=source_event.resource_id,
            metadata={**source_event.metadata, "demo": True},
            detected_at=datetime.utcnow(),
        )
    else:
        event = ErrorEvent(
            source=EventSource.CLOUDWATCH,
            error_type="ECS_TASK_STOPPED",
            title="ECS task stopped unexpectedly",
            description=(
                "Task arn:aws:ecs:us-east-1:123456789012:task/wordpress-prod/demo exited "
                "with code 1. Container 'wordpress' OOMKilled — memory limit 512 MiB exceeded."
            ),
            service="wordpress-prod",
            resource_id="arn:aws:ecs:us-east-1:123456789012:task/wordpress-prod/demo",
            metadata={
                "cluster": "wordpress-prod",
                "task_definition": "wordpress:42",
                "stopped_reason": "Essential container in task exited",
                "exit_code": 1,
                "demo": True,
            },
            detected_at=datetime.utcnow(),
        )

    await event_queue.enqueue(event)

    return {
        "event_id": event.id,
        "title": event.title,
        "service": event.service,
        "source": origin,
        "message": "Event injected — watch the Agents tab for live progress",
    }
