"""
GET /agents/status — current agent health snapshot.
"""
from fastapi import APIRouter

from app.services.agent_tracker import agent_tracker

router = APIRouter(prefix="/agents", tags=["agents"])


@router.get("/status")
async def get_agent_status():
    """Return active runs, recent errors, and per-agent stats for today."""
    return agent_tracker.snapshot()
