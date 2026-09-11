"""
Latency + dedup-gate health metrics API.

GET /metrics/latency          — p25/p50/p75/p95/p99 for every agent + pipeline stages
GET /metrics/latency/agents   — per-agent breakdown only
GET /metrics/latency/pipeline — pipeline stage breakdown only
GET /metrics/dedup            — dedup-gate health: outcome timeseries, per-layer
                                 latency percentiles, RAG error rate, duplicate-leak
                                 ground-truth check
"""
from typing import Any, Dict, List

from fastapi import APIRouter

from app.services.dedup_metrics import dedup_metrics
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


@router.get("/dedup")
async def get_dedup_health() -> Dict[str, Any]:
    """
    Dedup-gate health snapshot:
      - summary: 24h outcome totals, block rate, RAG error rate, duplicate-leak
        count/healthy flag
      - timeseries: hourly outcome counts (last 48h) for trend graphs
      - latency: p25/p50/p75/p95 per gate stage (layer1_sql, layer2_sql,
        layer3_rag_search, layer3_live_lookup)
    """
    return {
        "summary": dedup_metrics.summary(),
        "timeseries": dedup_metrics.timeseries(),
        "latency": dedup_metrics.latency_percentiles(),
    }
