"""
Detection layer — polls all infrastructure pillars on an interval and emits
standardized ErrorEvents to the queue.

Pillars:
  1. CloudWatch / ECS  — task crashes, CPU spikes
  2. Digital Ocean     — droplet status, WordPress site HTTP checks
  3. Cloudflare        — error rate spikes per zone
"""
from __future__ import annotations

import asyncio
import logging
from typing import List

from app.core.config import settings
from app.models.events import ErrorEvent, EventSource, Severity
from app.services.aws import AWSService
from app.services.cloudflare_service import cloudflare_service
from app.services.digitalocean import do_service
from app.services.event_queue import event_queue

logger = logging.getLogger(__name__)

# Detection thresholds
ECS_CPU_THRESHOLD_PCT = 85.0
ECS_MEMORY_THRESHOLD_PCT = 90.0
SITE_SLOW_RESPONSE_MS = 3_000.0
CF_ERROR_RATE_WARN = 0.05   # 5%
CF_ERROR_RATE_CRIT = 0.15   # 15%


def _parse_sites() -> List[dict]:
    """
    WORDPRESS_SITES env var format (comma-separated):
        https://site1.com|123|wp-01, https://site2.com|124|wp-02
    Falls back to plain URL if no pipe separators.
    """
    raw: str = getattr(settings, "wordpress_sites", "")
    if not raw:
        return []
    sites = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "|" in entry:
            parts = entry.split("|", 2)
            sites.append({
                "url": parts[0].strip(),
                "droplet_id": int(parts[1].strip()) if len(parts) > 1 else 0,
                "droplet_name": parts[2].strip() if len(parts) > 2 else "unknown",
            })
        else:
            sites.append({"url": entry, "droplet_id": 0, "droplet_name": "unknown"})
    return sites


class DetectionService:
    """Orchestrates all detection pillars. Call run_forever() as a background task."""

    def __init__(self):
        self._aws = AWSService()
        self._sites = _parse_sites()
        self._poll_interval: int = int(getattr(settings, "detection_poll_interval_seconds", 60))
        self._running = False

    # ------------------------------------------------------------------ #
    # Pillar 1 — ECS / CloudWatch
    # ------------------------------------------------------------------ #
    async def _detect_ecs(self) -> List[ErrorEvent]:
        events: List[ErrorEvent] = []
        cluster: str = getattr(settings, "ecs_cluster", "")
        raw_services: str = getattr(settings, "ecs_services", "")
        if not cluster or not raw_services:
            return events

        service_names = [s.strip() for s in raw_services.split(",") if s.strip()]
        for svc in service_names:
            try:
                status = self._aws.get_ecs_status(cluster, svc)

                if status.running_count < status.desired_count:
                    severity = Severity.P0 if status.running_count == 0 else Severity.P1
                    events.append(ErrorEvent(
                        source=EventSource.CLOUDWATCH,
                        severity=severity,
                        title=f"ECS task count below desired: {svc}",
                        description=(
                            f"Running {status.running_count}/{status.desired_count} tasks"
                        ),
                        service=svc,
                        resource_id=f"{cluster}/{svc}",
                        metadata={
                            "cluster": cluster,
                            "running": status.running_count,
                            "desired": status.desired_count,
                            "events": status.events[:3],
                        },
                    ))

            except Exception as exc:
                logger.warning("ECS detection failed for %s: %s", svc, exc)

        return events

    # ------------------------------------------------------------------ #
    # Pillar 2 — Digital Ocean
    # ------------------------------------------------------------------ #
    async def _detect_digitalocean(self) -> List[ErrorEvent]:
        events: List[ErrorEvent] = []

        # Droplet status
        try:
            droplets = await do_service.get_droplets()
            for d in droplets:
                if d.status != "active":
                    events.append(ErrorEvent(
                        source=EventSource.DIGITAL_OCEAN,
                        severity=Severity.P1,
                        title=f"Droplet offline: {d.name}",
                        description=f"Droplet {d.name} (id={d.id}) is '{d.status}'",
                        service=d.name,
                        resource_id=str(d.id),
                        metadata={"droplet_id": d.id, "region": d.region, "status": d.status},
                    ))
        except Exception as exc:
            logger.warning("DO droplet check failed: %s", exc)

        # WordPress site HTTP checks
        if self._sites:
            try:
                site_statuses = await do_service.check_all_sites(self._sites)
                for s in site_statuses:
                    if not s.is_healthy:
                        events.append(ErrorEvent(
                            source=EventSource.DIGITAL_OCEAN,
                            severity=Severity.P0,
                            title=f"Site down: {s.site_url}",
                            description=s.error or f"HTTP {s.status_code}",
                            service=s.droplet_name,
                            resource_id=s.site_url,
                            metadata={
                                "url": s.site_url,
                                "status_code": s.status_code,
                                "error": s.error,
                            },
                        ))
                    elif s.response_time_ms and s.response_time_ms > SITE_SLOW_RESPONSE_MS:
                        events.append(ErrorEvent(
                            source=EventSource.DIGITAL_OCEAN,
                            severity=Severity.P2,
                            title=f"Slow response: {s.site_url}",
                            description=(
                                f"{s.response_time_ms:.0f}ms "
                                f"(threshold {SITE_SLOW_RESPONSE_MS:.0f}ms)"
                            ),
                            service=s.droplet_name,
                            resource_id=s.site_url,
                            metadata={
                                "url": s.site_url,
                                "response_time_ms": s.response_time_ms,
                            },
                        ))
            except Exception as exc:
                logger.warning("Site health checks failed: %s", exc)

        return events

    # ------------------------------------------------------------------ #
    # Pillar 3 — Cloudflare
    # ------------------------------------------------------------------ #
    async def _detect_cloudflare(self) -> List[ErrorEvent]:
        events: List[ErrorEvent] = []
        try:
            status = await cloudflare_service.get_all_zones(since_minutes=5)
            for zone in status.zones:
                if zone.error_rate >= CF_ERROR_RATE_WARN:
                    severity = (
                        Severity.P0 if zone.error_rate >= CF_ERROR_RATE_CRIT else Severity.P1
                    )
                    events.append(ErrorEvent(
                        source=EventSource.CLOUDFLARE,
                        severity=severity,
                        title=f"High 5xx rate: {zone.zone_name}",
                        description=(
                            f"{zone.error_rate * 100:.1f}% error rate over last 5 min"
                            f" ({zone.http_5xx:,} errors / {zone.total_requests:,} requests)"
                        ),
                        service=zone.zone_name,
                        resource_id=zone.zone_id,
                        metadata={
                            "zone_id": zone.zone_id,
                            "error_rate": zone.error_rate,
                            "http_5xx": zone.http_5xx,
                            "total_requests": zone.total_requests,
                            "cache_hit_rate": zone.cache_hit_rate,
                        },
                    ))
        except Exception as exc:
            logger.warning("Cloudflare detection failed: %s", exc)

        return events

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    async def poll_once(self) -> List[ErrorEvent]:
        """Run all pillars concurrently, enqueue results."""
        results = await asyncio.gather(
            self._detect_ecs(),
            self._detect_digitalocean(),
            self._detect_cloudflare(),
            return_exceptions=True,
        )
        all_events: List[ErrorEvent] = []
        for result in results:
            if isinstance(result, list):
                all_events.extend(result)
            elif isinstance(result, Exception):
                logger.error("Detection pillar raised: %s", result)

        for event in all_events:
            try:
                event_queue.enqueue_nowait(event)
                logger.info("[%s] Enqueued: %s", event.severity, event.title)
            except Exception:
                logger.warning("Event queue full — dropping: %s", event.title)

        return all_events

    async def run_forever(self) -> None:
        """Background task: poll all pillars every poll_interval seconds."""
        self._running = True
        logger.info("Detection service started (interval=%ds)", self._poll_interval)
        while self._running:
            try:
                events = await self.poll_once()
                if events:
                    logger.info("Detection cycle: %d event(s)", len(events))
            except Exception as exc:
                logger.error("Detection cycle error: %s", exc)
            await asyncio.sleep(self._poll_interval)

    def stop(self) -> None:
        self._running = False


# Module-level singleton
detection_service = DetectionService()
