"""
Drift detection API.

GET /drift           current drift status + daily stats for last 30 days
GET /drift/stats     per-day success-rate breakdown only
"""
from typing import Any, Dict, List

from fastapi import APIRouter, Query

from app.services.drift_detector import drift_detector

router = APIRouter(prefix="/drift", tags=["drift"])


@router.get("")
async def get_drift() -> Dict[str, Any]:
    """
    Return the current drift assessment and a 30-day daily breakdown.

    has_drift=true means the success rate has regressed significantly
    against the 7-day baseline.
    """
    result = drift_detector.current_drift()
    stats  = drift_detector.daily_stats(n_days=30)
    return {
        "drift": {
            "has_drift":         result.has_drift,
            "current_rate":      result.current_rate,
            "baseline_rate":     result.baseline_rate,
            "drop":              result.drop,
            "current_samples":   result.current_samples,
            "baseline_samples":  result.baseline_samples,
            "message":           result.message,
        },
        "daily_stats": [
            {
                "date":         s.date,
                "approved":     s.approved,
                "rejected":     s.rejected,
                "total":        s.total,
                "success_rate": s.success_rate,
            }
            for s in stats
        ],
    }


@router.get("/stats")
async def get_drift_stats(
    n_days: int = Query(default=30, ge=1, le=365),
) -> List[Dict[str, Any]]:
    """Per-day fix-decision breakdown for the last *n_days* days."""
    return [
        {
            "date":         s.date,
            "approved":     s.approved,
            "rejected":     s.rejected,
            "total":        s.total,
            "success_rate": s.success_rate,
        }
        for s in drift_detector.daily_stats(n_days=n_days)
    ]
