"""
Detection layer — polls all infrastructure pillars on an interval and emits
standardized ErrorEvents to the queue.

Pillars:
  1. CloudWatch / ECS  — task crashes, CPU spikes
  2. EC2               — instance state, status checks, CPU spikes
  3. Digital Ocean     — droplet status, WordPress site HTTP checks
  4. Cloudflare        — error rate spikes per zone
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import List

from app.api.websocket_dashboard import broadcast
from app.core.config import settings
from app.models.events import ErrorEvent, EventSource, Severity
from app.services.aws import AWSService
from app.services.cloudflare_service import cloudflare_service
from app.services.digitalocean import do_service
from app.services.event_queue import event_queue
from app.services.pending_events import pending_event_store

logger = logging.getLogger(__name__)

# Matches camelCase/lowerCamel object fields whose name contains "Error" or
# "Exception" but whose value is falsy (false, null, undefined).
# e.g. "clarifyTimeoutError: false", "authorizationError: null"
# These are status flags logged by the application, not actual errors.
_FALSY_ERROR_FIELD_RE = re.compile(
    r'\b[a-z]\w*(?:Error|Exception)\s*:\s*(?:false|null|undefined)\b'
)

_SKIP_MARKERS = (
    # Moderation / classification payloads
    "publish_decision", "human_reviewer_note", "LLM check completed",
    "risk_score", "suspicious_signals",
    # WordPress / HTML interview content
    "<p><strong>", "<br/>", "<br />", "rendered:", "excerpt:",
    # AllInterviews-specific content fields
    "panelAnswer", "previewPanelAnswer", "postInfo {",
)

_EXC_CLASS_RE = re.compile(r'\b([A-Z][a-zA-Z0-9]*(?:Error|Exception|Fault|Warning))\b')
_KEYWORDS = ("FATAL", "CRITICAL", "EXCEPTION", "ERROR", "Error", "app crashed")


def classify_ecs_log(msg: str) -> tuple[str, str] | None:
    """Classify a raw ECS log message.

    Returns ``(error_type, category)`` if the message is a real error, or
    ``None`` if it should be skipped (false positive / structured status field).

    Used by both the background detection poller and the on-demand scan endpoint
    so they stay in sync.
    """
    if any(marker in msg for marker in _SKIP_MARKERS):
        return None
    if _FALSY_ERROR_FIELD_RE.search(msg):
        return None

    # Check for crash before regex — backward context prepended by get_error_logs()
    # may contain exception class names (TypeError etc.) that would shadow the crash.
    if "app crashed" in msg:
        return "APP_CRASHED", "crash"

    exc_match = _EXC_CLASS_RE.search(msg)
    if exc_match:
        error_type = exc_match.group(1).upper()
    else:
        error_type = next(
            (kw for kw in _KEYWORDS if kw in msg),
            "ECS_ERROR",
        ).upper().replace(" ", "_")

    if "APP_CRASHED" in error_type:
        category = "crash"
    elif any(m in error_type for m in ("WARNING", "DEPRECATION", "TIMEOUT", "CONNECTION")):
        category = "non_error"
    else:
        category = "error"

    return error_type, category


# Detection thresholds
ECS_CPU_THRESHOLD_PCT = 85.0
ECS_MEMORY_THRESHOLD_PCT = 90.0
EC2_CPU_THRESHOLD_PCT = 85.0
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
        ec2_region = getattr(settings, "ec2_region", "") or None
        self._aws_ec2 = AWSService(region=ec2_region) if ec2_region else self._aws
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
    # Pillar 1b — ECS task-based clusters (no services)
    # ------------------------------------------------------------------ #
    async def _detect_ecs_task_clusters(self) -> List[ErrorEvent]:
        events: List[ErrorEvent] = []
        raw: str = getattr(settings, "ecs_task_clusters", "")
        if not raw:
            return events

        clusters = [c.strip() for c in raw.split(",") if c.strip()]
        for cluster in clusters:
            try:
                result = self._aws.get_ecs_cluster_tasks(cluster)
                for failure in result["recent_failures"]:
                    events.append(ErrorEvent(
                        source=EventSource.CLOUDWATCH,
                        severity=Severity.P2,
                        title=f"ECS task failed: {cluster}",
                        description=failure["stopped_reason"] or failure["stop_code"] or "Task stopped unexpectedly",
                        service=cluster,
                        resource_id=f"{cluster}/{failure['task_id']}",
                        metadata={
                            "cluster": cluster,
                            "task_id": failure["task_id"],
                            "stop_code": failure["stop_code"],
                            "stopped_at": failure["stopped_at"],
                        },
                    ))
            except Exception as exc:
                logger.warning("ECS task cluster detection failed for %s: %s", cluster, exc)

        return events

    # ------------------------------------------------------------------ #
    # Pillar 2 — EC2
    # ------------------------------------------------------------------ #
    async def _detect_ec2(self) -> List[ErrorEvent]:
        events: List[ErrorEvent] = []
        raw: str = getattr(settings, "ec2_instance_ids", "")
        if not raw:
            return events

        instance_ids = [i.strip() for i in raw.split(",") if i.strip()]
        for instance_id in instance_ids:
            try:
                status = self._aws_ec2.get_ec2_status(instance_id)

                if status.state != "running":
                    events.append(ErrorEvent(
                        source=EventSource.CLOUDWATCH,
                        severity=Severity.P0,
                        title=f"EC2 instance not running: {instance_id}",
                        description=f"Instance {instance_id} ({status.instance_type}) is '{status.state}'",
                        service=instance_id,
                        resource_id=instance_id,
                        metadata={
                            "instance_id": instance_id,
                            "instance_type": status.instance_type,
                            "state": status.state,
                            "status_checks": status.status_checks,
                        },
                    ))
                elif "impaired" in status.status_checks:
                    events.append(ErrorEvent(
                        source=EventSource.CLOUDWATCH,
                        severity=Severity.P1,
                        title=f"EC2 status check failed: {instance_id}",
                        description=f"Status checks: {status.status_checks}",
                        service=instance_id,
                        resource_id=instance_id,
                        metadata={
                            "instance_id": instance_id,
                            "instance_type": status.instance_type,
                            "status_checks": status.status_checks,
                        },
                    ))
                elif status.cpu_utilization is not None and status.cpu_utilization > EC2_CPU_THRESHOLD_PCT:
                    events.append(ErrorEvent(
                        source=EventSource.CLOUDWATCH,
                        severity=Severity.P2,
                        title=f"EC2 high CPU: {instance_id}",
                        description=f"CPU at {status.cpu_utilization}% (threshold {EC2_CPU_THRESHOLD_PCT}%)",
                        service=instance_id,
                        resource_id=instance_id,
                        metadata={
                            "instance_id": instance_id,
                            "instance_type": status.instance_type,
                            "cpu_utilization": status.cpu_utilization,
                        },
                    ))

            except Exception as exc:
                logger.warning("EC2 detection failed for %s: %s", instance_id, exc)

        return events

    # ------------------------------------------------------------------ #
    # Pillar 3 — Digital Ocean
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
    # Pillar 5 — ECS log group error scan (general errors)
    # ------------------------------------------------------------------ #
    async def _detect_ecs_log_errors(self, window_minutes: int | None = None) -> List[ErrorEvent]:
        """Scan ECS_LOG_GROUPS for ERROR/Exception/FATAL entries.

        Configured via ECS_LOG_GROUPS env var (comma-separated log group names):
          ECS_LOG_GROUPS=/ecs/TaskAllInterviews,/ecs/OtherService

        Emits at most 5 distinct error events per log group to avoid flooding
        the pipeline. TriageAgent sets severity.
        """
        events: List[ErrorEvent] = []
        raw: str = getattr(settings, "ecs_log_groups", "")
        if not raw:
            return events

        minutes = window_minutes if window_minutes is not None else max(self._poll_interval // 60, 5)
        log_groups = [g.strip() for g in raw.split(",") if g.strip()]
        region = getattr(settings, "ecs_log_groups_region", "") or None

        for log_group in log_groups:
            service = log_group.rstrip("/").split("/")[-1]
            try:
                matches = self._aws.get_error_logs(log_group, minutes=minutes, limit=50, region=region)
                if not matches:
                    continue

                seen: set[str] = set()
                for log in matches:
                    msg = log["message"]
                    classified = classify_ecs_log(msg)
                    if classified is None:
                        continue
                    error_type, category = classified
                    normalized = re.sub(r'\b\d+\b', 'N', msg[:120]).strip()
                    sig = normalized[:80]
                    if sig in seen:
                        continue
                    seen.add(sig)

                    stream_parts = log["stream"].rsplit("/", 1)
                    task_id = stream_parts[-1] if len(stream_parts) > 1 else log["stream"]

                    events.append(ErrorEvent(
                        source=EventSource.CLOUDWATCH,
                        severity=None,
                        error_type=error_type,
                        task_id=task_id,
                        title=f"{error_type} in {service}",
                        description=msg[:3000],
                        service=service,
                        resource_id=log_group,
                        category=category,
                        metadata={
                            "log_group": log_group,
                            "task_id": task_id,
                            "timestamp": log["timestamp"],
                            "match_count": len(matches),
                        },
                    ))

                    if len(seen) >= 5:
                        break

            except Exception as exc:
                logger.warning("ECS log error detection failed for %s: %s", log_group, exc)

        return events

    # ------------------------------------------------------------------ #
    # Pillar 6 — CloudWatch log filter patterns (application-level errors)
    # ------------------------------------------------------------------ #
    async def _detect_cloudwatch_log_filters(self, window_minutes: int | None = None) -> List[ErrorEvent]:
        """Emit ErrorEvents for application errors matched by custom CloudWatch filter patterns.

        Configured via CW_LOG_FILTERS env var (JSON array):
          [{"log_group": "/ecs/allinterviews", "pattern": "NoSuchKey",
            "error_type": "S3_NO_SUCH_KEY", "service": "allinterviews"}]

        severity is intentionally left null — TriageAgent sets it.
        """
        events: List[ErrorEvent] = []
        raw: str = getattr(settings, "cw_log_filters", "")
        if not raw:
            return events

        try:
            filters = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("CW_LOG_FILTERS is not valid JSON — skipping pillar")
            return events

        window_minutes = window_minutes if window_minutes is not None else max(self._poll_interval // 60, 1)

        for f in filters:
            log_group = f.get("log_group", "")
            pattern = f.get("pattern", "")
            if not log_group or not pattern:
                continue

            error_type = f.get("error_type", pattern.upper().replace(" ", "_"))
            service = f.get("service", log_group)
            # Optional per-filter routing tag. Lets you add e.g. an "ECONNRESET"
            # pattern that lands in Non-errors without code change. Defaults to
            # "error" so existing configs are unaffected.
            category = f.get("category", "error")

            try:
                matches = self._aws.search_log_events(
                    log_group=log_group,
                    filter_pattern=pattern,
                    minutes=window_minutes,
                    limit=10,
                )
                if not matches:
                    continue

                latest = matches[0]
                # ECS log stream names end with the task ID (prefix/container/task-id)
                stream_parts = latest["stream"].rsplit("/", 1)
                task_id = stream_parts[-1] if len(stream_parts) > 1 else latest["stream"]

                events.append(ErrorEvent(
                    source=EventSource.CLOUDWATCH,
                    severity=None,          # TriageAgent sets this
                    error_type=error_type,
                    task_id=task_id,
                    title=f"{error_type}: {service}",
                    description=latest["message"][:300],
                    service=service,
                    resource_id=log_group,
                    category=category,
                    metadata={
                        "log_group": log_group,
                        "pattern": pattern,
                        "match_count": len(matches),
                        "latest_timestamp": latest["timestamp"],
                        "task_id": task_id,
                    },
                ))
            except Exception as exc:
                logger.warning("CW log filter check failed [%s / %s]: %s", log_group, pattern, exc)

        return events

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    async def poll_once(self, window_minutes: int | None = None) -> List[ErrorEvent]:
        """Run all pillars concurrently, enqueue results."""
        # ECS log pulling is re-enabled (5-min cadence) to feed fix-agents with
        # per-line error events from monitored services. The SNS webhook path
        # produced only generic "alarm fired" cards; polling gives per-line
        # fidelity (real error text, distinct signatures) which is what
        # triage/diagnosis agents need. CloudWatch Logs filter_log_events at
        # this cadence is well within rate limits (0.003 TPS vs 5 TPS).
        # DigitalOcean / Cloudflare pillars stay disabled — separate concern.
        results = await asyncio.gather(
            self._detect_ecs(),
            self._detect_ecs_task_clusters(),
            self._detect_ec2(),
            # self._detect_digitalocean(),     # disabled
            # self._detect_cloudflare(),       # disabled
            self._detect_ecs_log_errors(window_minutes=window_minutes),
            self._detect_cloudwatch_log_filters(window_minutes=window_minutes),
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
                # Crashes (process exits) bypass the approval gate — they are
                # high-severity enough that human review before diagnosis adds
                # unnecessary delay. All other events wait for manual approval.
                if event.category == "crash":
                    await event_queue.enqueue(event)
                    await broadcast({"type": "crash_auto_queued", "title": event.title})
                    logger.info("[CRASH] Auto-queued for pipeline: %s", event.title)
                    continue

                pe, is_new = pending_event_store.add(event)
                if pe is None:
                    continue
                msg_type = "pending_event_added" if is_new else "pending_event_updated"
                await broadcast({"type": msg_type, "event": pending_event_store.serialize(pe)})
                if is_new:
                    logger.info("[%s] Pending approval: %s", event.severity, event.title)
                else:
                    logger.debug("[%s] Duplicate (%dx): %s", event.severity, pe.occurrences, event.title)
            except Exception:
                logger.warning("Failed to queue for approval: %s", event.title)

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
