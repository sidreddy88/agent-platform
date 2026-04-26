"""
Logs API — fetch recent error logs from CloudWatch log groups.
"""
from typing import Any, Dict

from fastapi import APIRouter, Query

from app.core.config import settings
from app.services.aws import AWSService

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("/ecs")
async def get_ecs_logs(minutes: int = Query(default=60, ge=5, le=1440)) -> Dict[str, Any]:
    """Return recent error log events from configured ECS log groups."""
    aws = AWSService()
    raw: str = getattr(settings, "ecs_log_groups", "")
    log_groups = [g.strip() for g in raw.split(",") if g.strip()] if raw else []

    results = []
    for group in log_groups:
        try:
            events = aws.get_error_logs(group, minutes=minutes)
            results.append({
                "log_group": group,
                "error_count": len(events),
                "events": events,
                "error": None,
            })
        except Exception as exc:
            results.append({
                "log_group": group,
                "error_count": 0,
                "events": [],
                "error": str(exc),
            })

    return {
        "log_groups": results,
        "total_errors": sum(r["error_count"] for r in results),
        "minutes": minutes,
    }
