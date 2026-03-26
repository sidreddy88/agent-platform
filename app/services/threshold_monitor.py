"""
Threshold monitor — periodically checks infrastructure metrics against thresholds
and fires Slack alerts via alerting_service.

Checks (every threshold_check_interval_seconds):
  - DO droplet memory > alert_do_memory_pct (default 85%)
  - DO droplet load_1 > vcpu count
  - ALB 5xx count > alert_alb_5xx_count (default 10 in last 5 min)
  - ECS running < desired

A per-resource cooldown (threshold_cooldown_minutes, default 30 min) prevents
repeat alerts for the same ongoing issue.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from app.core.config import settings
from app.services.alerting import Alert, Severity, alerting_service
from app.services.aws import AWSService
from app.services.digitalocean import do_service

logger = logging.getLogger(__name__)


class ThresholdMonitor:
    """Runs threshold checks on a fixed interval with per-resource cooldowns."""

    def __init__(self) -> None:
        self._running = False
        self._cooldowns: dict[str, datetime] = {}
        self._aws = AWSService()
        ec2_region = getattr(settings, "ec2_region", "") or None
        self._aws_alb = AWSService(region=ec2_region) if ec2_region else self._aws

    # ------------------------------------------------------------------
    # Cooldown helpers
    # ------------------------------------------------------------------

    def _in_cooldown(self, key: str) -> bool:
        last = self._cooldowns.get(key)
        if last is None:
            return False
        cooldown = timedelta(minutes=int(getattr(settings, "threshold_cooldown_minutes", 30)))
        return datetime.now(timezone.utc) - last < cooldown

    def _set_cooldown(self, key: str) -> None:
        self._cooldowns[key] = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Check: Digital Ocean memory + load
    # ------------------------------------------------------------------

    async def _check_do(self) -> None:
        mem_threshold = float(getattr(settings, "alert_do_memory_pct", 85.0))
        try:
            droplets = await do_service.get_droplets()
            metrics_list = await asyncio.gather(
                *[do_service.get_droplet_metrics(d.id) for d in droplets],
                return_exceptions=True,
            )
            for d, m in zip(droplets, metrics_list):
                if not isinstance(m, dict):
                    continue

                mem = m.get("memory_percent")
                load = m.get("load_1")

                # Memory threshold
                if mem is not None and mem > mem_threshold:
                    key = f"do_memory_{d.id}"
                    if not self._in_cooldown(key):
                        await alerting_service.send_alert(Alert(
                            severity=Severity.WARNING if mem < 95 else Severity.ERROR,
                            title=f"High memory: {d.name}",
                            message=(
                                f"Droplet *{d.name}* ({d.region}) memory is at *{mem:.1f}%* "
                                f"(threshold: {mem_threshold:.0f}%). "
                                f"Size: {d.size}. Consider restarting services or resizing."
                            ),
                            source="ThresholdMonitor",
                            metadata={"droplet": d.name, "memory_pct": mem, "threshold": mem_threshold},
                        ))
                        self._set_cooldown(key)

                # Load threshold (load_1 > vCPU count)
                if load is not None and load > d.vcpus:
                    key = f"do_load_{d.id}"
                    if not self._in_cooldown(key):
                        await alerting_service.send_alert(Alert(
                            severity=Severity.WARNING,
                            title=f"High load: {d.name}",
                            message=(
                                f"Droplet *{d.name}* ({d.region}) 1-min load is *{load:.2f}* "
                                f"which exceeds vCPU count ({d.vcpus}). "
                                f"Size: {d.size}."
                            ),
                            source="ThresholdMonitor",
                            metadata={"droplet": d.name, "load_1": load, "vcpus": d.vcpus},
                        ))
                        self._set_cooldown(key)

        except Exception as exc:
            logger.warning("DO threshold check failed: %s", exc)

    # ------------------------------------------------------------------
    # Check: ALB 5xx
    # ------------------------------------------------------------------

    async def _check_alb(self) -> None:
        raw: str = getattr(settings, "alb_names", "")
        alb_names = [n.strip() for n in raw.split(",") if n.strip()] if raw else []
        threshold = int(getattr(settings, "alert_alb_5xx_count", 10))

        for name in alb_names:
            try:
                status = self._aws.get_alb_status(name)
                if status.http_5xx is not None and status.http_5xx > threshold:
                    key = f"alb_5xx_{name}"
                    if not self._in_cooldown(key):
                        await alerting_service.send_alert(Alert(
                            severity=Severity.ERROR,
                            title=f"ALB 5xx spike: {name}",
                            message=(
                                f"Load balancer *{name}* has *{status.http_5xx}* 5xx errors "
                                f"in the last 5 minutes (threshold: {threshold}). "
                                f"Requests: {status.request_count or 0}. "
                                f"Healthy targets: {status.healthy_targets}/{status.total_targets}."
                            ),
                            source="ThresholdMonitor",
                            metadata={
                                "alb": name,
                                "http_5xx": status.http_5xx,
                                "threshold": threshold,
                                "healthy_targets": status.healthy_targets,
                            },
                        ))
                        self._set_cooldown(key)
            except Exception as exc:
                logger.warning("ALB threshold check failed for %s: %s", name, exc)

    # ------------------------------------------------------------------
    # Check: ECS running < desired
    # ------------------------------------------------------------------

    async def _check_ecs(self) -> None:
        cluster: str = getattr(settings, "ecs_cluster", "")
        raw: str = getattr(settings, "ecs_services", "")
        if not cluster or not raw:
            return

        service_names = [s.strip() for s in raw.split(",") if s.strip()]
        for svc in service_names:
            try:
                status = self._aws.get_ecs_status(cluster, svc)
                if status.running_count < status.desired_count:
                    key = f"ecs_tasks_{cluster}_{svc}"
                    if not self._in_cooldown(key):
                        severity = Severity.ERROR if status.running_count == 0 else Severity.WARNING
                        await alerting_service.send_alert(Alert(
                            severity=severity,
                            title=f"ECS tasks below desired: {svc}",
                            message=(
                                f"Service *{svc}* in cluster *{cluster}* is running "
                                f"*{status.running_count}/{status.desired_count}* tasks. "
                                + ("No tasks running!" if status.running_count == 0 else "")
                            ),
                            source="ThresholdMonitor",
                            metadata={
                                "service": svc,
                                "cluster": cluster,
                                "running": status.running_count,
                                "desired": status.desired_count,
                            },
                        ))
                        self._set_cooldown(key)
            except Exception as exc:
                logger.warning("ECS threshold check failed for %s: %s", svc, exc)

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    async def run_once(self) -> None:
        await asyncio.gather(
            self._check_do(),
            self._check_alb(),
            self._check_ecs(),
            return_exceptions=True,
        )

    async def run_forever(self) -> None:
        self._running = True
        interval = int(getattr(settings, "threshold_check_interval_seconds", 300))
        logger.info("ThresholdMonitor started (interval=%ds)", interval)
        while self._running:
            try:
                await self.run_once()
            except Exception as exc:
                logger.error("ThresholdMonitor cycle error: %s", exc)
            await asyncio.sleep(interval)

    def stop(self) -> None:
        self._running = False


threshold_monitor = ThresholdMonitor()
