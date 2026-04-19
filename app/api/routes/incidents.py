"""
Incident feed API — list, filter, and inspect incidents.

POST /incidents/trigger  — inject a test ErrorEvent directly into the pipeline
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.models.events import ErrorEvent, EventSource
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
    """MTTD/MTTR, false positive rate, totals."""
    return incident_store.metrics()


@router.delete("")
async def clear_incidents() -> Dict[str, Any]:
    """Delete all incidents from the store (memory + disk)."""
    count = incident_store.clear()
    return {"deleted": count}


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
