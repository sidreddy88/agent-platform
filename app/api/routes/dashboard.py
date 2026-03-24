"""
Dashboard API — unified view of all 4 pillars + queue + incident metrics.
"""
import asyncio
from typing import Any, Dict

from fastapi import APIRouter

from app.core.config import settings
from app.services.aws import AWSService
from app.services.cloudflare_service import cloudflare_service
from app.services.digitalocean import do_service
from app.services.event_queue import event_queue
from app.services.incident_store import incident_store

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("")
async def get_dashboard() -> Dict[str, Any]:
    """
    Returns live data for all 4 pillars:
      - ECS clusters + service health
      - Digital Ocean droplets
      - Cloudflare zone metrics
      - Incident + queue stats
    """
    aws = AWSService()
    cluster: str = getattr(settings, "ecs_cluster", "")
    raw_services: str = getattr(settings, "ecs_services", "")
    service_names = [s.strip() for s in raw_services.split(",") if s.strip()] if raw_services else []

    # --- ECS ---
    async def _ecs_pillar():
        results = []
        for svc in service_names:
            try:
                status = aws.get_ecs_status(cluster, svc)
                results.append({
                    "service": svc,
                    "cluster": cluster,
                    "running": status.running_count,
                    "desired": status.desired_count,
                    "pending": status.pending_count,
                    "status": status.status,
                    "healthy": status.running_count >= status.desired_count,
                    "recent_events": status.events[:3] if status.events else [],
                })
            except Exception as exc:
                results.append({"service": svc, "cluster": cluster, "error": str(exc), "healthy": False})
        return results

    # --- Digital Ocean ---
    async def _do_pillar():
        try:
            droplets = await do_service.get_droplets()
            return {
                "droplets": [
                    {
                        "id": d.id,
                        "name": d.name,
                        "status": d.status,
                        "healthy": d.status == "active",
                        "region": d.region,
                        "ip": d.ip_address,
                        "size": d.size,
                    }
                    for d in droplets
                ],
                "total": len(droplets),
                "healthy": sum(1 for d in droplets if d.status == "active"),
            }
        except Exception as exc:
            return {"error": str(exc), "droplets": [], "total": 0, "healthy": 0}

    # --- Cloudflare ---
    async def _cf_pillar():
        try:
            cf = await cloudflare_service.get_all_zones(since_minutes=30)
            return {
                "zones": [
                    {
                        "zone_name": z.zone_name,
                        "total_requests": z.total_requests,
                        "error_rate": round(z.error_rate, 4),
                        "cache_hit_rate": round(z.cache_hit_rate, 4),
                        "http_5xx": z.http_5xx,
                        "http_4xx": z.http_4xx,
                        "threats_blocked": z.threats_blocked,
                        "healthy": z.error_rate < 0.05,
                    }
                    for z in cf.zones
                ],
                "total_requests": cf.total_requests,
                "overall_error_rate": round(cf.overall_error_rate, 4),
                "overall_cache_hit_rate": round(cf.overall_cache_hit_rate, 4),
                "healthy": cf.overall_error_rate < 0.05,
            }
        except Exception as exc:
            return {"error": str(exc), "zones": [], "healthy": False}

    ecs, do_data, cf_data = await asyncio.gather(_ecs_pillar(), _do_pillar(), _cf_pillar())

    return {
        "ecs": ecs,
        "digital_ocean": do_data,
        "cloudflare": cf_data,
        "queue": event_queue.stats,
        "incidents": incident_store.metrics(),
    }
