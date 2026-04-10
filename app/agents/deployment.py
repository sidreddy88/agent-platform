"""
DeploymentAgent — monitors AWS infrastructure health and diagnoses issues.

Flow (driven by ReAct loop in BaseAgent):
  1. check_health      → overall health sweep across all configured services
  2. get_ecs_status    → task counts, deployment state, recent events
  3. get_ec2_status    → instance state, CPU utilization, status checks
  4. get_service_logs  → recent CloudWatch logs with error/warning summary
  5. get_metrics       → CloudWatch metric datapoints for any service

Issues detected:
  TASK_FAILING      — ECS tasks failing to start or crash-looping
  CAPACITY_ISSUE    — running < desired (under-provisioned / OOMKilled)
  HIGH_CPU          — EC2/ECS CPU above threshold
  HIGH_MEMORY       — memory utilization above threshold
  ERROR_SPIKE       — error rate in logs above baseline
  DEPLOYMENT_ISSUE  — recent deployment left service in degraded state
  INSTANCE_IMPAIRED — EC2 status check failure
"""

import json
from dataclasses import asdict
from datetime import datetime, timezone, timedelta

from app.agents.base import AgentResult, BaseAgent
from app.services.aws import AWSError, AWSService
from app.services.llm import LLMService

# Thresholds for anomaly detection
CPU_WARN_PCT = 80.0
CPU_CRIT_PCT = 95.0
ERROR_RATE_WARN = 10       # errors per minute
CAPACITY_RATIO_WARN = 0.8  # running/desired below this → degraded
ELB_5XX_WARN = 10          # 5xx errors in last 5 min


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_ecs(status) -> str:
    ratio = (
        f"{status.running_count}/{status.desired_count}"
        f" ({'OK' if status.running_count >= status.desired_count else 'DEGRADED'})"
    )
    lines = [
        f"ECS Service: {status.cluster}/{status.service}",
        f"  Status      : {status.status}",
        f"  Tasks       : running/desired = {ratio}  pending={status.pending_count}",
        f"  Deployment  : {status.deployment_status}",
        "",
        "  Recent deployments:",
    ]
    for d in status.deployments:
        lines.append(
            f"    [{d['id']}] {d['status']} rollout={d['rollout']} "
            f"running={d['running']}/{d['desired']}"
        )
    lines.append("\n  Recent events:")
    for e in status.events:
        lines.append(f"    - {e}")
    return "\n".join(lines)


def _fmt_ec2(status) -> str:
    cpu_str = f"{status.cpu_utilization}%" if status.cpu_utilization is not None else "N/A"
    warning = ""
    if status.cpu_utilization and status.cpu_utilization >= CPU_CRIT_PCT:
        warning = "  ⚠ CRITICAL CPU"
    elif status.cpu_utilization and status.cpu_utilization >= CPU_WARN_PCT:
        warning = "  ⚠ HIGH CPU"
    return (
        f"EC2 Instance: {status.instance_id}\n"
        f"  State         : {status.state}\n"
        f"  Type          : {status.instance_type}\n"
        f"  Public IP     : {status.public_ip or 'N/A'}\n"
        f"  Private IP    : {status.private_ip or 'N/A'}\n"
        f"  CPU (5min)    : {cpu_str}{warning}\n"
        f"  Status checks : {status.status_checks}"
    )


def _fmt_logs(summary) -> str:
    error_rate = round(summary.error_count / max(summary.minutes, 1), 2)
    warning = ""
    if error_rate >= ERROR_RATE_WARN:
        warning = f"  ⚠ HIGH ERROR RATE ({error_rate}/min)"
    lines = [
        f"Logs: {summary.log_group} (last {summary.minutes} min)",
        f"  Events   : {summary.total_events}",
        f"  Errors   : {summary.error_count}  ({error_rate}/min){warning}",
        f"  Warnings : {summary.warning_count}",
    ]
    if summary.recent_errors:
        lines.append("\n  Recent errors:")
        for e in summary.recent_errors[-5:]:
            lines.append(f"    {e[:200]}")
    return "\n".join(lines)


def _fmt_alb(status) -> str:
    if not status.healthy:
        icon = "✗"
    elif status.unhealthy_targets > 0:
        icon = "⚠"
    else:
        icon = "✓"
    lines = [
        f"ALB: {status.name}  [{icon} {status.state}]",
        f"  DNS             : {status.dns_name}",
        f"  Targets         : {status.healthy_targets} healthy / {status.unhealthy_targets} unhealthy / {status.total_targets} total",
    ]
    if status.request_count is not None:
        lines.append(f"  Requests (5min) : {status.request_count}")
    if status.http_5xx is not None:
        warn = "  ⚠ HIGH 5XX" if status.http_5xx >= ELB_5XX_WARN else ""
        lines.append(f"  5xx errors (5min): {status.http_5xx}{warn}")
    return "\n".join(lines)


def _fmt_metric(metric) -> str:
    dim_str = ", ".join(f"{k}={v}" for k, v in metric.dimensions.items())
    lines = [
        f"Metric: {metric.namespace}/{metric.metric_name} [{dim_str}]",
        f"  Average : {metric.average}",
        f"  Maximum : {metric.maximum}",
        f"  Points  : {len(metric.datapoints)}",
    ]
    for dp in metric.datapoints[-5:]:
        lines.append(f"    {dp['timestamp']}  avg={dp['average']}  max={dp['maximum']} {dp['unit']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def get_ecs_status(
    cluster: str,
    service: str,
    aws: AWSService,
) -> str:
    """Fetch ECS service health: task counts, deployment state, recent events."""
    try:
        status = aws.get_ecs_status(cluster, service)
        failures = aws.get_ecs_task_failures(cluster, service)
    except AWSError as exc:
        return f"AWS error: {exc}"

    result = _fmt_ecs(status)

    if failures:
        result += "\n\n  Stopped task failures (last 10):"
        for f in failures[:5]:
            result += f"\n    Task {f['task_id']}: {f['stopped_reason'] or f['stop_code']}"
            for cr in f["container_reasons"]:
                result += f"\n      Container {cr['name']}: exit={cr['exit_code']} {cr['reason']}"

    # Cache for check_health
    aws._cached_ecs = aws._cached_ecs if hasattr(aws, "_cached_ecs") else {}
    aws._cached_ecs[f"{cluster}/{service}"] = status
    return result


async def get_ec2_status(instance_id: str, aws: AWSService) -> str:
    """Fetch EC2 instance state, CPU utilization, and status checks."""
    try:
        status = aws.get_ec2_status(instance_id)
    except AWSError as exc:
        return f"AWS error: {exc}"

    aws._cached_ec2 = aws._cached_ec2 if hasattr(aws, "_cached_ec2") else {}
    aws._cached_ec2[instance_id] = status
    return _fmt_ec2(status)


async def get_service_logs(
    log_group: str,
    aws: AWSService,
    minutes: int = 30,
) -> str:
    """Fetch recent CloudWatch logs and summarise error/warning counts."""
    try:
        summary = aws.get_service_logs(log_group, minutes)
    except AWSError as exc:
        return f"AWS error: {exc}"

    aws._cached_logs = aws._cached_logs if hasattr(aws, "_cached_logs") else {}
    aws._cached_logs[log_group] = summary
    return _fmt_logs(summary)


async def get_metrics(
    namespace: str,
    metric_name: str,
    dimensions: dict[str, str],
    aws: AWSService,
    minutes: int = 60,
) -> str:
    """Fetch CloudWatch metric datapoints (average + max) over the last N minutes."""
    try:
        metric = aws.get_metrics(namespace, metric_name, dimensions, minutes=minutes)
    except AWSError as exc:
        return f"AWS error: {exc}"

    return _fmt_metric(metric)


async def check_health(
    resources: list[dict],
    aws: AWSService,
    llm: LLMService,
) -> str:
    """
    Run a health sweep across all provided resources and produce a summary.

    resources is a list of dicts, each with a "type" key:
      {"type": "ecs",  "cluster": "prod", "service": "api"}
      {"type": "ec2",  "instance_id": "i-0abc123"}
      {"type": "logs", "log_group": "/app/prod", "minutes": 30}
    """
    sections: list[str] = []
    issues: list[str] = []

    for r in resources:
        rtype = r.get("type", "")
        if rtype == "ecs":
            out = await get_ecs_status(r["cluster"], r["service"], aws)
            sections.append(out)
            cached = getattr(aws, "_cached_ecs", {}).get(f"{r['cluster']}/{r['service']}")
            if cached:
                if cached.running_count < cached.desired_count:
                    issues.append(
                        f"CAPACITY_ISSUE: {r['service']} running {cached.running_count}/{cached.desired_count} tasks"
                    )
                if cached.deployment_status == "degraded":
                    issues.append(f"DEPLOYMENT_ISSUE: {r['service']} deployment is degraded")

        elif rtype == "ec2":
            out = await get_ec2_status(r["instance_id"], aws)
            sections.append(out)
            cached = getattr(aws, "_cached_ec2", {}).get(r["instance_id"])
            if cached:
                if cached.state != "running":
                    issues.append(f"INSTANCE_DOWN: {r['instance_id']} state={cached.state}")
                if cached.cpu_utilization and cached.cpu_utilization >= CPU_CRIT_PCT:
                    issues.append(f"HIGH_CPU: {r['instance_id']} cpu={cached.cpu_utilization}%")
                if "impaired" in (cached.status_checks or ""):
                    issues.append(f"INSTANCE_IMPAIRED: {r['instance_id']} status={cached.status_checks}")

        elif rtype == "logs":
            out = await get_service_logs(r["log_group"], aws, r.get("minutes", 30))
            sections.append(out)
            cached = getattr(aws, "_cached_logs", {}).get(r["log_group"])
            if cached:
                rate = cached.error_count / max(cached.minutes, 1)
                if rate >= ERROR_RATE_WARN:
                    issues.append(
                        f"ERROR_SPIKE: {r['log_group']} {cached.error_count} errors in {cached.minutes}min"
                    )

        elif rtype == "alb":
            alb_name = r.get("name", "")
            try:
                status = aws.get_alb_status(alb_name)
                sections.append(_fmt_alb(status))
                if status.unhealthy_targets > 0:
                    issues.append(
                        f"UNHEALTHY_HOSTS: {alb_name} — {status.unhealthy_targets} unhealthy "
                        f"target(s) ({status.healthy_targets}/{status.total_targets} healthy)"
                    )
                if status.healthy_targets == 0:
                    issues.append(f"NO_HEALTHY_HOSTS: {alb_name} — all targets are unhealthy")
                if status.state != "active":
                    issues.append(f"ALB_IMPAIRED: {alb_name} state={status.state}")
                if status.http_5xx and status.http_5xx >= ELB_5XX_WARN:
                    issues.append(f"HIGH_5XX_RATE: {alb_name} {status.http_5xx} 5xx errors in last 5min")
            except AWSError as exc:
                sections.append(f"ALB {alb_name}: could not fetch — {exc}")
                issues.append(f"ALB_FETCH_ERROR: {alb_name} — {exc}")

    raw_data = "\n\n".join(sections)
    issues_str = "\n".join(f"  - {i}" for i in issues) if issues else "  None detected"

    prompt = f"""You are a senior DevOps/SRE engineer reviewing infrastructure health data.

RAW HEALTH DATA:
{raw_data}

AUTO-DETECTED ISSUES:
{issues_str}

Write a concise health report in this format:

# Infrastructure Health Report
**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

## Overall Status
<HEALTHY / DEGRADED / CRITICAL> — <one sentence summary>

## Services
<For each service/instance: name, status icon (✓/⚠/✗), one-line summary>

## Issues Found
<Bullet list with severity (CRITICAL/HIGH/MEDIUM/LOW), what it is, and impact.
 Write "None" if everything is healthy.>

## Recommended Actions
<Numbered list of concrete actions to take, most urgent first.
 Write "None required" if healthy.>

## Details
<Any notable observations from the raw data worth flagging>

Be direct. An on-call engineer should be able to act on this within 60 seconds."""

    return await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system="You are a senior SRE writing a concise, actionable infrastructure health report.",
    )


# ---------------------------------------------------------------------------
# DeploymentAgent
# ---------------------------------------------------------------------------

class DeploymentAgent(BaseAgent):
    """
    Monitors AWS infrastructure health and diagnoses deployment issues.

    Usage:
        agent = DeploymentAgent()

        # Ask a natural language question
        result = await agent.run("Is the prod API service healthy?")

        # Or pass structured resources to check
        result = await agent.run(json.dumps({
            "question": "Check overall health",
            "resources": [
                {"type": "ecs",  "cluster": "prod", "service": "api"},
                {"type": "ec2",  "instance_id": "i-0abc123"},
                {"type": "logs", "log_group": "/app/prod", "minutes": 30},
            ]
        }))

        print(result.answer)
    """

    def __init__(self, aws: AWSService | None = None) -> None:
        super().__init__()
        self._aws = aws or AWSService()
        self._register_tools()

    def _register_tools(self) -> None:
        aws = self._aws
        llm = self._llm

        async def _get_ecs_status(cluster: str, service: str) -> str:
            return await get_ecs_status(cluster, service, aws)

        async def _get_ec2_status(instance_id: str) -> str:
            return await get_ec2_status(instance_id, aws)

        async def _get_service_logs(log_group: str, minutes: int = 30) -> str:
            return await get_service_logs(log_group, aws, minutes)

        async def _get_metrics(
            namespace: str,
            metric_name: str,
            dimensions: dict,
            minutes: int = 60,
        ) -> str:
            return await get_metrics(namespace, metric_name, dimensions, aws, minutes)

        async def _get_alb_status(name: str) -> str:
            try:
                status = aws.get_alb_status(name)
            except AWSError as exc:
                return f"AWS error: {exc}"
            return _fmt_alb(status)

        async def _check_health(resources: list) -> str:
            return await check_health(resources, aws, llm)

        self.register_tool(
            "get_ecs_status",
            _get_ecs_status,
            (
                "Get ECS service health: running vs desired task count, deployment status, "
                "stopped task failure reasons, and recent service events. "
                "Input: {cluster: string, service: string}"
            ),
        )
        self.register_tool(
            "get_ec2_status",
            _get_ec2_status,
            (
                "Get EC2 instance state, CPU utilization (last 5 min), and status checks. "
                "Input: {instance_id: string}"
            ),
        )
        self.register_tool(
            "get_service_logs",
            _get_service_logs,
            (
                "Fetch recent CloudWatch logs from a log group and summarise error/warning counts. "
                "Input: {log_group: string, minutes: integer (optional, default 30)}"
            ),
        )
        self.register_tool(
            "get_metrics",
            _get_metrics,
            (
                "Fetch CloudWatch metric datapoints (average + max) for any AWS service. "
                "Common namespaces: AWS/ECS, AWS/EC2, AWS/ApplicationELB, AWS/RDS. "
                "Input: {namespace: string, metric_name: string, "
                "dimensions: {key: value}, minutes: integer (optional, default 60)}"
            ),
        )
        self.register_tool(
            "get_alb_status",
            _get_alb_status,
            (
                "Get Application Load Balancer health: state (active/impaired), "
                "healthy vs unhealthy target counts, request count, and 5xx error count (last 5 min). "
                "Input: {name: string (ALB name)}"
            ),
        )
        self.register_tool(
            "check_health",
            _check_health,
            (
                "Run a full health sweep across a list of resources (ECS services, EC2 instances, "
                "ALBs, CloudWatch log groups) and produce a structured health report. "
                "Use this when asked for an overall health summary. "
                "Input: {resources: [{type: 'ecs'|'ec2'|'alb'|'logs', ...resource-specific fields}]}"
                " For ALB: {type: 'alb', name: 'my-alb-name'}"
            ),
        )

    async def run(self, user_input: str) -> AgentResult:
        """
        Run the deployment agent.

        Accepts:
          - Natural language: "Is the prod API healthy?"
          - JSON with resources:
            {"question": "...", "resources": [{type: "ecs", cluster: "...", service: "..."}]}
        """
        try:
            params = json.loads(user_input)
            question = params.get("question", "Check overall infrastructure health")
            resources = params.get("resources", [])
            resources_str = json.dumps(resources, indent=2) if resources else "(not specified)"
            prompt = (
                f"{question}\n\n"
                f"Resources to check:\n{resources_str}\n\n"
                "Use the available tools to gather health data, then produce a complete "
                "health report. Use check_health for a full sweep, or individual tools "
                "for targeted questions. Detect: task failures, high CPU/memory, "
                "error spikes in logs, and deployment issues."
            )
        except (json.JSONDecodeError, KeyError):
            prompt = (
                user_input + "\n\n"
                "Use the available tools to check infrastructure health. "
                "Detect: task failures, high CPU/memory, error spikes in logs, deployment issues."
            )

        return await super().run(prompt)
