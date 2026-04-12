"""
Latency metrics API.

GET /metrics/latency          — p50/p95/p99 for every agent + pipeline stages
GET /metrics/latency/agents   — per-agent breakdown only
GET /metrics/latency/pipeline — pipeline stage breakdown only
"""
from typing import Any, Dict, List

from fastapi import APIRouter

from app.services.latency import latency_tracker

router = APIRouter(prefix="/metrics", tags=["metrics"])


@router.get("/latency")
async def get_latency() -> Dict[str, Any]:
    """
    p50/p95/p99/min/max/mean latency (ms) for:
      - every agent tracked so far (rolling 500-sample window)
      - each pipeline stage derived from incident timestamps
    """
    return latency_tracker.summary()


@router.get("/latency/agents")
async def get_agent_latency() -> List[Dict[str, Any]]:
    """Per-agent latency percentiles only."""
    return latency_tracker.all_percentiles()


@router.get("/latency/pipeline")
async def get_pipeline_latency() -> List[Dict[str, Any]]:
    """
    Pipeline stage latency percentiles derived from incident timestamps:
      triage, diagnosis, fix, mttr (end-to-end)
    """
    return latency_tracker.pipeline_stage_percentiles()
