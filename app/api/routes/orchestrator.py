"""
Orchestrator dashboard API.

GET  /orchestrator/stats   — lane utilization, counters, in-flight set
GET  /orchestrator/routes  — last 200 routing decisions (newest first)
"""
from typing import Any, Dict, List

from fastapi import APIRouter

from app.services.orchestrator import orchestrator

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])


@router.get("/stats")
async def get_stats() -> Dict[str, Any]:
    """Orchestrator counters and priority-lane semaphore utilization."""
    return orchestrator.stats()


@router.get("/routes")
async def get_routes() -> List[Dict[str, Any]]:
    """Most recent routing decisions, newest first."""
    decisions = list(orchestrator.route_log)
    decisions.reverse()
    return [
        {
            "event_id": d.event_id,
            "event_title": d.event_title,
            "service": d.service,
            "pipeline": d.pipeline,
            "priority": d.priority,
            "enrichment": d.enrichment,
            "dedup_skipped": d.dedup_skipped,
            "routed_at": d.routed_at.isoformat(),
        }
        for d in decisions
    ]
