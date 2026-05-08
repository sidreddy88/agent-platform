"""
Performance observability — surfaces Atlas Performance Advisor data
(slow queries + suggested indexes) inside the agent-platform UI so the
heaviest queries sit alongside the error feed.

GET /performance/heaviest

Pure observability — no event ingestion, no pipeline integration. The
response is cached in-memory for 60s so a polling UI doesn't hammer the
Atlas API.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter

from app.core.config import settings
from app.services.mongodb_atlas import atlas_service

router = APIRouter(prefix="/performance", tags=["performance"])
logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, Any] = {"payload": None, "fetched_at_monotonic": 0.0}
_cache_lock = asyncio.Lock()


async def _build_payload(hours: int) -> Dict[str, Any]:
    slow, indexes = await atlas_service.get_performance_advisor(hours=hours)
    return {
        "slow_queries": [asdict(q) for q in slow],
        "suggested_indexes": [asdict(i) for i in indexes],
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "atlas_project_id": settings.atlas_project_id or "",
        "configured": bool(
            settings.atlas_public_key and settings.atlas_private_key and settings.atlas_project_id
        ),
        "hours": hours,
    }


@router.get("/heaviest")
async def heaviest_api_calls(hours: int = 24, refresh: bool = False) -> Dict[str, Any]:
    """Return Atlas Performance Advisor data for the project.

    Args:
        hours:   Lookback window passed to Atlas (default 24h, max 168h).
        refresh: Bypass the 60s cache and fetch fresh.

    The response shape is stable so the UI can render it directly.
    """
    hours = max(1, min(168, int(hours)))

    now = asyncio.get_event_loop().time()
    cached = _cache["payload"]
    if (
        not refresh
        and cached is not None
        and cached.get("hours") == hours
        and (now - _cache["fetched_at_monotonic"]) < _CACHE_TTL_SECONDS
    ):
        return cached

    async with _cache_lock:
        # Re-check after acquiring the lock — another concurrent caller may
        # have populated the cache while we were waiting.
        now = asyncio.get_event_loop().time()
        cached = _cache["payload"]
        if (
            not refresh
            and cached is not None
            and cached.get("hours") == hours
            and (now - _cache["fetched_at_monotonic"]) < _CACHE_TTL_SECONDS
        ):
            return cached

        payload = await _build_payload(hours)
        _cache["payload"] = payload
        _cache["fetched_at_monotonic"] = asyncio.get_event_loop().time()
        return payload
