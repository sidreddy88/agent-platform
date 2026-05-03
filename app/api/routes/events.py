"""
Pending event approval API.

GET  /events/pending          — list events awaiting approval
POST /events/{id}/approve     — approve one event → enters pipeline
POST /events/approve-all      — approve all pending events
POST /events/{id}/dismiss     — discard without queuing
"""
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from app.api.websocket_dashboard import broadcast
from app.services.event_queue import event_queue
from app.services.pending_events import pending_event_store

router = APIRouter(prefix="/events", tags=["events"])


@router.get("/pending")
async def list_pending() -> List[Dict[str, Any]]:
    return [pending_event_store.serialize(pe) for pe in pending_event_store.list_all()]


@router.post("/{event_id}/approve")
async def approve_event(event_id: str) -> Dict[str, Any]:
    pe = pending_event_store.remove(event_id)
    if not pe:
        raise HTTPException(status_code=404, detail="Pending event not found")
    await event_queue.enqueue(pe._event)
    await broadcast({"type": "pending_event_removed", "id": event_id})
    return {"status": "queued", "event_id": event_id}


@router.post("/approve-all")
async def approve_all_events() -> Dict[str, Any]:
    events = pending_event_store.clear()
    for pe in events:
        await event_queue.enqueue(pe._event)
    await broadcast({"type": "pending_events_cleared"})
    return {"status": "queued", "count": len(events)}


@router.post("/{event_id}/dismiss")
async def dismiss_event(event_id: str) -> Dict[str, Any]:
    pe = pending_event_store.dismiss(event_id)
    if not pe:
        raise HTTPException(status_code=404, detail="Pending event not found")
    await broadcast({"type": "pending_event_removed", "id": event_id})
    return {"status": "dismissed", "event_id": event_id}
