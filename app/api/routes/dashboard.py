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
    ec2_region = getattr(settings, "ec2_region", "") or None
    aws_ec2 = AWSService(region=ec2_region) if ec2_region else aws
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

    # --- ECS task clusters ---
    async def _ecs_task_pillar():
        raw: str = getattr(settings, "ecs_task_clusters", "")
        clusters = [c.strip() for c in raw.split(",") if c.strip()] if raw else []
        results = []
        for cluster in clusters:
            try:
                data = aws.get_ecs_cluster_tasks(cluster)
                results.append({
                    "cluster": cluster,
                    "running_tasks": data["running_tasks"],
                    "recent_failures": len(data["recent_failures"]),
                    "healthy": len(data["recent_failures"]) == 0,
                })
            except Exception as exc:
                results.append({"cluster": cluster, "error": str(exc), "healthy": False})
        return results

    # --- EC2 ---
    async def _ec2_pillar():
        raw_ids: str = getattr(settings, "ec2_instance_ids", "")
        instance_ids = [i.strip() for i in raw_ids.split(",") if i.strip()] if raw_ids else []
        results = []
        for instance_id in instance_ids:
            try:
                status = aws_ec2.get_ec2_status(instance_id)
                results.append({
                    "instance_id": status.instance_id,
                    "state": status.state,
                    "instance_type": status.instance_type,
                    "public_ip": status.public_ip,
                    "private_ip": status.private_ip,
                    "cpu_utilization": status.cpu_utilization,
                    "status_checks": status.status_checks,
                    "healthy": status.state == "running" and "impaired" not in status.status_checks,
                })
            except Exception as exc:
                results.append({"instance_id": instance_id, "error": str(exc), "healthy": False})
        return {
            "instances": results,
            "total": len(results),
            "healthy": sum(1 for i in results if i.get("healthy")),
        }

    # --- Digital Ocean ---
    async def _do_pillar():
        try:
            droplets = await do_service.get_droplets()
            metrics_list = await asyncio.gather(
                *[do_service.get_droplet_metrics(d.id) for d in droplets],
                return_exceptions=True,
            )
            result = []
            for d, m in zip(droplets, metrics_list):
                metrics = m if isinstance(m, dict) else {}
                result.append({
                    "id": d.id,
                    "name": d.name,
                    "status": d.status,
                    "healthy": d.status == "active",
                    "region": d.region,
                    "ip": d.ip_address,
                    "size": d.size,
                    "load_1": metrics.get("load_1"),
                    "memory_percent": metrics.get("memory_percent"),
                })
            return {
                "droplets": result,
                "total": len(result),
                "healthy": sum(1 for d in result if d["healthy"]),
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

    # --- ALB ---
    async def _alb_pillar():
        raw: str = getattr(settings, "alb_names", "")
        names = [n.strip() for n in raw.split(",") if n.strip()] if raw else []
        results = []
        for name in names:
            try:
                s = aws.get_alb_status(name)
                results.append({
                    "name": s.name,
                    "dns_name": s.dns_name,
                    "state": s.state,
                    "healthy_targets": s.healthy_targets,
                    "unhealthy_targets": s.unhealthy_targets,
                    "total_targets": s.total_targets,
                    "request_count": s.request_count,
                    "http_5xx": s.http_5xx,
                    "healthy": s.healthy,
                })
            except Exception as exc:
                results.append({"name": name, "error": str(exc), "healthy": False})
        return results

    ecs, ecs_tasks, ec2, do_data, cf_data, alb = await asyncio.gather(
        _ecs_pillar(), _ecs_task_pillar(), _ec2_pillar(), _do_pillar(), _cf_pillar(), _alb_pillar()
    )

    return {
        "ecs": ecs,
        "ecs_task_clusters": ecs_tasks,
        "ec2": ec2,
        "digital_ocean": do_data,
        "cloudflare": cf_data,
        "alb": alb,
        "queue": event_queue.stats,
        "incidents": incident_store.metrics(),
    }
