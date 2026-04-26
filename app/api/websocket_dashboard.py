"""
Real-time WebSocket endpoint for the dashboard.
WS /ws/dashboard

Sends:
  {"type": "init", "incidents": [...], "queue": {...}}   — on connect
  {"type": "event", "event": {...}}                      — on new ErrorEvent
  {"type": "incident_update", "incident": {...}}         — on incident state change
  {"type": "pong"}                                       — on client ping
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Dict, Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.services.agent_tracker import agent_tracker
from app.services.event_queue import event_queue
from app.services.incident_store import incident_store
from app.services.pending_events import pending_event_store

router = APIRouter()
logger = logging.getLogger(__name__)

_clients: Set[WebSocket] = set()


async def broadcast(message: Dict[str, Any]) -> None:
    """Send a message to all connected dashboard clients."""
    dead: Set[WebSocket] = set()
    for ws in list(_clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)


@router.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    _clients.add(websocket)
    logger.info("Dashboard WS connected (%d total)", len(_clients))

    try:
        # Send initial state
        await websocket.send_json({
            "type": "init",
            "incidents": [
                {**i.model_dump(mode="json"), "mttr_seconds": i.mttr_seconds, "age_seconds": i.age_seconds}
                for i in incident_store.list_active()
            ],
            "metrics": incident_store.metrics(),
            "queue": event_queue.stats,
            "agents": agent_tracker.snapshot(),
            "pending_events": [
                pending_event_store.serialize(pe) for pe in pending_event_store.list_all()
            ],
            "timestamp": datetime.utcnow().isoformat(),
        })

        # Keep-alive loop — client sends "ping" every 25s
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=35)
                if raw == "ping":
                    await websocket.send_json({"type": "pong"})
            except asyncio.TimeoutError:
                # No ping received — close connection
                break
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(websocket)
        logger.info("Dashboard WS disconnected (%d total)", len(_clients))
