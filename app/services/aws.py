"""
AWS service — wraps boto3 calls for ECS, EC2, and CloudWatch.

Clients are created lazily per-call using a thread-local session so the
service is safe to use from asyncio (boto3 is synchronous; callers should
run_in_executor for truly async workloads, but for agent tool use the
simplicity of direct calls is fine).

Credentials are resolved in the standard boto3 order:
  1. Explicit keys in settings (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY)
  2. Environment variables
  3. ~/.aws/credentials
  4. IAM instance / task role
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ECSServiceStatus:
    cluster: str
    service: str
    status: str                  # ACTIVE / INACTIVE / DRAINING
    running_count: int
    desired_count: int
    pending_count: int
    deployment_status: str       # healthy / degraded / deploying
    deployments: list[dict]      # recent deployments
    events: list[str]            # last 5 service events


@dataclass
class EC2InstanceStatus:
    instance_id: str
    state: str                   # running / stopped / terminated / …
    instance_type: str
    public_ip: str | None
    private_ip: str | None
    cpu_utilization: float | None   # % over last 5 min, None if unavailable
    status_checks: str           # ok / impaired / insufficient-data


@dataclass
class CloudWatchMetric:
    namespace: str
    metric_name: str
    dimensions: dict[str, str]
    datapoints: list[dict]       # [{timestamp, value, unit}]
    average: float | None
    maximum: float | None



@dataclass
class LogSummary:
    log_group: str
    minutes: int
    total_events: int
    error_count: int
    warning_count: int
    recent_errors: list[str]     # last 10 error lines
    sample_lines: list[str]      # last 20 lines regardless of level


# ---------------------------------------------------------------------------
# Error
# ---------------------------------------------------------------------------

@dataclass
class ALBStatus:
    name: str
    dns_name: str
    state: str                    # active / provisioning / active_impaired / failed
    healthy_targets: int
    unhealthy_targets: int
    total_targets: int
    request_count: int | None     # last 5 min
    http_5xx: int | None          # last 5 min
    healthy: bool



class AWSError(Exception):
    def __init__(self, service: str, message: str) -> None:
        super().__init__(f"AWS {service} error: {message}")
        self.service = service


# ---------------------------------------------------------------------------
# AWSService
# ---------------------------------------------------------------------------

class AWSService:
    """
    Thin async-friendly wrapper around boto3 for ECS, EC2, and CloudWatch.

    Uses local AWS credentials by default. Override via .env:
        AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
    """

    def __init__(self, region: str | None = None) -> None:
        self._region = region or settings.aws_region
        self._session_kwargs: dict[str, Any] = {"region_name": self._region}
        if settings.aws_access_key_id:
            self._session_kwargs["aws_access_key_id"] = settings.aws_access_key_id
        if settings.aws_secret_access_key:
            self._session_kwargs["aws_secret_access_key"] = settings.aws_secret_access_key

    def _client(self, service: str):
        return boto3.client(service, **self._session_kwargs)

    # ------------------------------------------------------------------
    # ECS
    # ------------------------------------------------------------------

    def get_ecs_status(self, cluster: str, service: str) -> ECSServiceStatus:
        """Return current health, task counts, and recent deployments for an ECS service."""
        ecs = self._client("ecs")
        try:
            resp = ecs.describe_services(cluster=cluster, services=[service])
        except (BotoCoreError, ClientError) as exc:
            raise AWSError("ECS", str(exc)) from exc

        if not resp["services"]:
            raise AWSError("ECS", f"Service '{service}' not found in cluster '{cluster}'")

        svc = resp["services"][0]

        # Determine deployment health
        deployments = svc.get("deployments", [])
        dep_status = "healthy"
        if any(d["rolloutState"] == "IN_PROGRESS" for d in deployments if "rolloutState" in d):
            dep_status = "deploying"
        elif svc["runningCount"] < svc["desiredCount"]:
            dep_status = "degraded"

        dep_summaries = [
            {
                "id": d["id"].split("/")[-1],
                "status": d["status"],
                "rollout": d.get("rolloutState", "N/A"),
                "running": d["runningCount"],
                "desired": d["desiredCount"],
                "created_at": d["createdAt"].isoformat() if isinstance(d.get("createdAt"), datetime) else str(d.get("createdAt", "")),
            }
            for d in deployments[:5]
        ]

        events = [e["message"] for e in svc.get("events", [])[:5]]

        return ECSServiceStatus(
            cluster=cluster,
            service=service,
            status=svc["status"],
            running_count=svc["runningCount"],
            desired_count=svc["desiredCount"],
            pending_count=svc["pendingCount"],
            deployment_status=dep_status,
            deployments=dep_summaries,
            events=events,
        )

    def get_ecs_cluster_tasks(self, cluster: str) -> dict:
        """Return running and recently stopped task counts for a task-based cluster (no services)."""
        ecs = self._client("ecs")
        try:
            running_arns = ecs.list_tasks(cluster=cluster, desiredStatus="RUNNING").get("taskArns", [])
            stopped_arns = ecs.list_tasks(cluster=cluster, desiredStatus="STOPPED").get("taskArns", [])

            failed = []
            if stopped_arns:
                tasks = ecs.describe_tasks(cluster=cluster, tasks=stopped_arns[:10])["tasks"]
                for t in tasks:
                    stop_code = t.get("stopCode", "")
                    stopped_reason = t.get("stoppedReason", "")
                    # Flag tasks that failed (not just completed normally)
                    if stop_code not in ("EssentialContainerExited",) or any(
                        c.get("exitCode") not in (0, None) for c in t.get("containers", [])
                    ):
                        failed.append({
                            "task_id": t["taskArn"].split("/")[-1],
                            "stop_code": stop_code,
                            "stopped_reason": stopped_reason,
                            "stopped_at": t["stoppedAt"].isoformat() if isinstance(t.get("stoppedAt"), datetime) else "",
                        })
        except (BotoCoreError, ClientError) as exc:
            raise AWSError("ECS", str(exc)) from exc

        return {
            "cluster": cluster,
            "running_tasks": len(running_arns),
            "recent_failures": failed,
        }

    def list_ecs_services(self, cluster: str) -> list[str]:
        """Return all service ARNs in a cluster."""
        ecs = self._client("ecs")
        try:
            paginator = ecs.get_paginator("list_services")
            arns = []
            for page in paginator.paginate(cluster=cluster):
                arns.extend(page["serviceArns"])
            return arns
        except (BotoCoreError, ClientError) as exc:
            raise AWSError("ECS", str(exc)) from exc

    def get_ecs_task_failures(self, cluster: str, service: str) -> list[dict]:
        """Return stopped tasks with their stop reason (useful for crash-loop detection)."""
        ecs = self._client("ecs")
        try:
            task_arns_resp = ecs.list_tasks(
                cluster=cluster, serviceName=service, desiredStatus="STOPPED"
            )
            task_arns = task_arns_resp.get("taskArns", [])
            if not task_arns:
                return []
            tasks = ecs.describe_tasks(cluster=cluster, tasks=task_arns[:10])["tasks"]
        except (BotoCoreError, ClientError) as exc:
            raise AWSError("ECS", str(exc)) from exc

        return [
            {
                "task_id": t["taskArn"].split("/")[-1],
                "stop_code": t.get("stopCode", ""),
                "stopped_reason": t.get("stoppedReason", ""),
                "stopped_at": t["stoppedAt"].isoformat() if isinstance(t.get("stoppedAt"), datetime) else "",
                "container_reasons": [
                    {"name": c["name"], "reason": c.get("reason", ""), "exit_code": c.get("exitCode")}
                    for c in t.get("containers", [])
                    if c.get("exitCode") not in (0, None) or c.get("reason")
                ],
            }
            for t in tasks
        ]

    # ------------------------------------------------------------------
    # EC2
    # ------------------------------------------------------------------

    def get_ec2_status(self, instance_id: str) -> EC2InstanceStatus:
        """Return state, IPs, and CPU utilization for an EC2 instance."""
        ec2 = self._client("ec2")
        cw = self._client("cloudwatch")

        try:
            resp = ec2.describe_instances(InstanceIds=[instance_id])
        except (BotoCoreError, ClientError) as exc:
            raise AWSError("EC2", str(exc)) from exc

        reservations = resp.get("Reservations", [])
        if not reservations or not reservations[0].get("Instances"):
            raise AWSError("EC2", f"Instance '{instance_id}' not found")

        inst = reservations[0]["Instances"][0]

        # Status checks
        try:
            sc_resp = ec2.describe_instance_status(InstanceIds=[instance_id])
            sc_list = sc_resp.get("InstanceStatuses", [])
            if sc_list:
                sys_check = sc_list[0]["SystemStatus"]["Status"]
                inst_check = sc_list[0]["InstanceStatus"]["Status"]
                status_checks = f"system={sys_check}, instance={inst_check}"
            else:
                status_checks = "no-status-data"
        except (BotoCoreError, ClientError):
            status_checks = "unknown"

        # CPU from CloudWatch (last 5 minutes, 1 datapoint)
        cpu = None
        try:
            now = datetime.now(timezone.utc)
            from datetime import timedelta
            cw_resp = cw.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName="CPUUtilization",
                Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                StartTime=now - timedelta(minutes=10),
                EndTime=now,
                Period=300,
                Statistics=["Average"],
            )
            dps = cw_resp.get("Datapoints", [])
            if dps:
                cpu = round(sorted(dps, key=lambda d: d["Timestamp"])[-1]["Average"], 2)
        except (BotoCoreError, ClientError):
            pass

        return EC2InstanceStatus(
            instance_id=instance_id,
            state=inst["State"]["Name"],
            instance_type=inst["InstanceType"],
            public_ip=inst.get("PublicIpAddress"),
            private_ip=inst.get("PrivateIpAddress"),
            cpu_utilization=cpu,
            status_checks=status_checks,
        )

    # ------------------------------------------------------------------
    # ALB
    # ------------------------------------------------------------------

    def get_alb_status(self, alb_name: str) -> ALBStatus:
        """Return health and basic traffic metrics for an Application Load Balancer."""
        elb = self._client("elbv2")
        cw = self._client("cloudwatch")
        from datetime import timedelta

        try:
            lb_resp = elb.describe_load_balancers(Names=[alb_name])
            lbs = lb_resp.get("LoadBalancers", [])
            if not lbs:
                raise AWSError("ALB", f"Load balancer '{alb_name}' not found")
            lb = lbs[0]
            lb_arn = lb["LoadBalancerArn"]
            dns_name = lb["DNSName"]
            state = lb["State"]["Code"]

            # Target group health
            tg_resp = elb.describe_target_groups(LoadBalancerArn=lb_arn)
            healthy = unhealthy = total = 0
            for tg in tg_resp.get("TargetGroups", []):
                health_resp = elb.describe_target_health(TargetGroupArn=tg["TargetGroupArn"])
                for t in health_resp.get("TargetHealthDescriptions", []):
                    total += 1
                    if t["TargetHealth"]["State"] == "healthy":
                        healthy += 1
                    else:
                        unhealthy += 1

        except (BotoCoreError, ClientError) as exc:
            raise AWSError("ALB", str(exc)) from exc

        # CloudWatch metrics — last 5 min
        now = datetime.now(timezone.utc)
        lb_dim = lb_arn.split("loadbalancer/")[-1]  # dimension value format

        def _cw_sum(metric: str) -> int | None:
            try:
                resp = cw.get_metric_statistics(
                    Namespace="AWS/ApplicationELB",
                    MetricName=metric,
                    Dimensions=[{"Name": "LoadBalancer", "Value": lb_dim}],
                    StartTime=now - timedelta(minutes=5),
                    EndTime=now,
                    Period=300,
                    Statistics=["Sum"],
                )
                dps = resp.get("Datapoints", [])
                return int(dps[-1]["Sum"]) if dps else 0
            except (BotoCoreError, ClientError):
                return None

        request_count = _cw_sum("RequestCount")
        http_5xx = _cw_sum("HTTPCode_Target_5XX_Count")

        is_healthy = state == "active" and healthy > 0
        return ALBStatus(
            name=alb_name,
            dns_name=dns_name,
            state=state,
            healthy_targets=healthy,
            unhealthy_targets=unhealthy,
            total_targets=total,
            request_count=request_count,
            http_5xx=http_5xx,
            healthy=is_healthy,
        )

    # ------------------------------------------------------------------
    # CloudWatch Metrics
    # ------------------------------------------------------------------

    def get_metrics(
        self,
        namespace: str,
        metric_name: str,
        dimensions: dict[str, str],
        minutes: int = 60,
        period: int = 300,
        statistics: list[str] | None = None,
    ) -> CloudWatchMetric:
        """Fetch CloudWatch metric datapoints for any namespace/metric."""
        cw = self._client("cloudwatch")
        stats = statistics or ["Average", "Maximum"]

        from datetime import timedelta
        now = datetime.now(timezone.utc)

        try:
            resp = cw.get_metric_statistics(
                Namespace=namespace,
                MetricName=metric_name,
                Dimensions=[{"Name": k, "Value": v} for k, v in dimensions.items()],
                StartTime=now - timedelta(minutes=minutes),
                EndTime=now,
                Period=period,
                Statistics=stats,
            )
        except (BotoCoreError, ClientError) as exc:
            raise AWSError("CloudWatch", str(exc)) from exc

        datapoints = sorted(
            [
                {
                    "timestamp": dp["Timestamp"].isoformat(),
                    "average": round(dp.get("Average", 0), 4),
                    "maximum": round(dp.get("Maximum", 0), 4),
                    "unit": dp.get("Unit", ""),
                }
                for dp in resp.get("Datapoints", [])
            ],
            key=lambda d: d["timestamp"],
        )

        averages = [dp["average"] for dp in datapoints if dp["average"]]
        maximums = [dp["maximum"] for dp in datapoints if dp["maximum"]]

        return CloudWatchMetric(
            namespace=namespace,
            metric_name=metric_name,
            dimensions=dimensions,
            datapoints=datapoints,
            average=round(sum(averages) / len(averages), 4) if averages else None,
            maximum=max(maximums) if maximums else None,
        )

    # ------------------------------------------------------------------
    # CloudWatch Logs
    # ------------------------------------------------------------------

    def search_log_events(
        self,
        log_group: str,
        filter_pattern: str,
        minutes: int = 5,
        limit: int = 100,
    ) -> list[dict]:
        """Search a CloudWatch log group for events matching a filter pattern.

        filter_pattern uses CloudWatch filter syntax — plain strings like "NoSuchKey"
        work as substring matches. Returns events newest-first.
        """
        logs = self._client("logs")
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        start_ms = int((now - timedelta(minutes=minutes)).timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)

        try:
            resp = logs.filter_log_events(
                logGroupName=log_group,
                startTime=start_ms,
                endTime=end_ms,
                filterPattern=filter_pattern,
                limit=limit,
            )
        except (BotoCoreError, ClientError) as exc:
            raise AWSError("CloudWatchLogs", str(exc)) from exc

        events = []
        for ev in resp.get("events", []):
            ts = datetime.fromtimestamp(ev["timestamp"] / 1000, tz=timezone.utc).isoformat()
            events.append({
                "timestamp": ts,
                "stream": ev.get("logStreamName", ""),
                "message": ev.get("message", "").rstrip(),
            })
        return sorted(events, key=lambda e: e["timestamp"], reverse=True)

    def get_error_logs(self, log_group: str, minutes: int = 60) -> list[dict]:
        """Fetch error-level log events from a CloudWatch log group.

        Paginates through all results via nextToken so no cap is applied.
        For each matching error line, fetches the next 20 lines from the same
        log stream within a 5-second window to capture Node.js / Python stack
        traces that appear on the lines immediately following the error.
        """
        logs = self._client("logs")
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        start_ms = int((now - timedelta(minutes=minutes)).timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)

        base_kwargs = dict(
            logGroupName=log_group,
            startTime=start_ms,
            endTime=end_ms,
            filterPattern='?"ERROR" ?"Error" ?"EXCEPTION" ?"Exception" ?"FATAL" ?"CRITICAL" ?"Traceback"',
        )

        _MAX_EVENTS = 200
        raw_events: list[dict] = []
        next_token: str | None = None
        while len(raw_events) < _MAX_EVENTS:
            try:
                kwargs = {**base_kwargs, **({"nextToken": next_token} if next_token else {})}
                resp = logs.filter_log_events(**kwargs)
            except (BotoCoreError, ClientError) as exc:
                raise AWSError("CloudWatchLogs", str(exc)) from exc
            raw_events.extend(resp.get("events", []))
            next_token = resp.get("nextToken")
            if not next_token:
                break
        raw_events = raw_events[:_MAX_EVENTS]

        events = []
        for ev in raw_events:
            ts = datetime.fromtimestamp(ev["timestamp"] / 1000, tz=timezone.utc).isoformat()
            message = ev.get("message", "").rstrip()

            # Fetch context lines after the error to capture stack traces.
            # Stack frames ("at functionName (/app/...)") appear on subsequent
            # lines and won't match the error filter pattern above.
            try:
                ctx_resp = logs.get_log_events(
                    logGroupName=log_group,
                    logStreamName=ev["logStreamName"],
                    startTime=ev["timestamp"],
                    endTime=ev["timestamp"] + 5000,  # 5-second window
                    limit=25,
                    startFromHead=True,
                )
                ctx_lines = [e.get("message", "").rstrip() for e in ctx_resp.get("events", [])]
                # Drop the first line if it's the error line itself (same message)
                if ctx_lines and ctx_lines[0].strip() == message.strip():
                    ctx_lines = ctx_lines[1:]
                if ctx_lines:
                    message = message + "\n" + "\n".join(ctx_lines[:20])
            except (BotoCoreError, ClientError):
                pass  # context is best-effort; proceed with just the error line

            events.append({
                "timestamp": ts,
                "stream": ev.get("logStreamName", ""),
                "message": message,
                "log_group": log_group,
            })

        return sorted(events, key=lambda e: e["timestamp"], reverse=True)

    def get_service_logs(self, log_group: str, minutes: int = 30) -> LogSummary:
        """Fetch recent logs from a CloudWatch log group and summarise errors."""
        logs = self._client("logs")
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        start_ms = int((now - timedelta(minutes=minutes)).timestamp() * 1000)
        end_ms = int(now.timestamp() * 1000)

        _error_re = re.compile(r"\b(error|exception|fatal|critical|traceback)\b", re.IGNORECASE)
        _warn_re = re.compile(r"\b(warn|warning)\b", re.IGNORECASE)

        all_lines: list[str] = []
        error_lines: list[str] = []
        error_count = 0
        warning_count = 0

        try:
            # List log streams, sorted by latest event
            streams_resp = logs.describe_log_streams(
                logGroupName=log_group,
                orderBy="LastEventTime",
                descending=True,
                limit=5,
            )
            streams = [s["logStreamName"] for s in streams_resp.get("logStreams", [])]

            for stream in streams:
                try:
                    events_resp = logs.get_log_events(
                        logGroupName=log_group,
                        logStreamName=stream,
                        startTime=start_ms,
                        endTime=end_ms,
                        limit=500,
                        startFromHead=False,
                    )
                    for ev in events_resp.get("events", []):
                        msg = ev.get("message", "").rstrip()
                        all_lines.append(msg)
                        if _error_re.search(msg):
                            error_count += 1
                            error_lines.append(msg)
                        elif _warn_re.search(msg):
                            warning_count += 1
                except (BotoCoreError, ClientError):
                    continue

        except (BotoCoreError, ClientError) as exc:
            raise AWSError("CloudWatchLogs", str(exc)) from exc

        return LogSummary(
            log_group=log_group,
            minutes=minutes,
            total_events=len(all_lines),
            error_count=error_count,
            warning_count=warning_count,
            recent_errors=error_lines[-10:],
            sample_lines=all_lines[-20:],
        )
