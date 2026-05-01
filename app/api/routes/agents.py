"""
GET  /agents/status  — current agent health snapshot.
POST /agents/demo    — inject the most recent ECS/CloudWatch error into the pipeline.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.events import ErrorEvent, EventSource, IncidentStatus
from app.services.agent_tracker import agent_tracker
from app.services.event_queue import event_queue
from app.services.incident_store import incident_store

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/status")
async def get_agent_status():
    """Return active runs, recent errors, and per-agent stats for today."""
    return agent_tracker.snapshot()


def _as_utc(dt: datetime) -> datetime:
    """Attach UTC to naive datetimes so arithmetic across tz-aware/naive pairs works."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _utc_iso(dt) -> str | None:
    """Serialize a naive UTC datetime to ISO 8601 with Z suffix."""
    if dt is None:
        return None
    s = dt.isoformat()
    return s if s.endswith("Z") or "+" in s else s + "Z"


@router.get("/prs")
async def get_agent_prs():
    """Return one entry per unique PR number, newest first."""
    skip = {IncidentStatus.DUPLICATE, IncidentStatus.NOISE}

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


@router.get("/pr-stats")
async def get_pr_stats():
    """
    Return enriched stats for all resolved agent PRs.

    Fetches PR description and CI check results from GitHub for each PR.
    GitHub calls are best-effort — fields are null if the API is unreachable.
    """
    from app.core.config import settings
    from app.services.github import GitHubService

    fix_repo = getattr(settings, "fix_target_repo", "")
    owner, repo = fix_repo.split("/", 1) if "/" in fix_repo else ("", "")
    gh = GitHubService()

    resolved = [
        i for i in incident_store.list_all()
        if i.status == IncidentStatus.RESOLVED and (i.pr_number or i.pr_url)
    ]

    results = []
    for incident in resolved:
        event = incident.error_event

        mttd_seconds = None
        if incident.pr_created_at and incident.detected_at:
            mttd_seconds = round(
                (_as_utc(incident.pr_created_at) - _as_utc(incident.detected_at)).total_seconds(), 1
            )

        pr_description = None
        ci_conclusion = None
        ci_checks: list[dict] = []

        if owner and incident.pr_number:
            try:
                pr_details = await gh.get_pr(owner, repo, incident.pr_number)
                pr_description = pr_details.description

                ci_checks = await gh.get_commit_checks(owner, repo, pr_details.head_sha)
                if ci_checks:
                    conclusions = {c["conclusion"] for c in ci_checks if c["conclusion"]}
                    if "failure" in conclusions or "timed_out" in conclusions:
                        ci_conclusion = "failure"
                    elif conclusions and conclusions <= {"success", "skipped", "neutral"}:
                        ci_conclusion = "success"
                    else:
                        ci_conclusion = "pending"
            except Exception:
                pass

        log_source = (
            event.metadata.get("log_group")
            or event.resource_id
            or None
        )

        results.append({
            "incident_id": incident.id,
            "pr_url": incident.pr_url,
            "pr_number": incident.pr_number,
            "service": event.service,
            "severity": str(event.severity).split(".")[-1] if event.severity else None,
            "error_title": event.title,
            "error_description": event.description,
            "log_source": log_source,
            "files_changed": incident.pr_files_changed,
            "pr_description": pr_description,
            "diagnosis": incident.diagnosis,
            "confidence": incident.confidence,
            "detected_at": _utc_iso(incident.detected_at),
            "pr_created_at": _utc_iso(incident.pr_created_at),
            "resolved_at": _utc_iso(incident.resolved_at),
            "mttd_seconds": mttd_seconds,
            "mttr_seconds": incident.mttr_seconds,
            "ci_conclusion": ci_conclusion,
            "ci_checks": ci_checks,
        })

    mttrs = [r["mttr_seconds"] for r in results if r["mttr_seconds"] is not None]
    confs = [r["confidence"] for r in results if r["confidence"] is not None]
    ci_done = [r for r in results if r["ci_conclusion"] in ("success", "failure")]

    summary = {
        "total": len(results),
        "avg_mttr_seconds": round(sum(mttrs) / len(mttrs), 1) if mttrs else None,
        "avg_confidence": round(sum(confs) / len(confs), 3) if confs else None,
        "ci_pass_rate": (
            round(sum(1 for r in ci_done if r["ci_conclusion"] == "success") / len(ci_done), 3)
            if ci_done else None
        ),
    }

    return {"prs": results, "summary": summary}


class RunNoteBody(BaseModel):
    note: str
    mark_failed: bool = False


@router.post("/runs/{run_id}/note")
async def annotate_run(run_id: str, body: RunNoteBody):
    """
    Attach a human-written note to an agent run and optionally mark it as failed.
    Works on any run — completed or already failed.
    Persists to the agent_runs table so it survives restarts.
    """
    from app.services.database import get_db
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Run not found")
        new_status = "failed" if body.mark_failed else row["status"]
        conn.execute(
            "UPDATE agent_runs SET error_message = ?, status = ? WHERE run_id = ?",
            (body.note.strip(), new_status, run_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Keep in-memory history in sync
    for run in agent_tracker._history:
        if run.run_id == run_id:
            run.error_message = body.note.strip()
            if body.mark_failed:
                run.status = "failed"
            break

    return {"status": "updated", "run_id": run_id}


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
