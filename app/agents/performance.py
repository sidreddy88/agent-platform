"""
PerformanceAgent — monitors application metrics, detects regressions, and recommends actions.

Flow (driven by ReAct loop in BaseAgent):
  1. get_latency_metrics        → p50/p95/p99 for recent window
  2. get_error_rates            → error % over time
  3. get_baseline               → rolling 7-day average for comparison
  4. detect_regression          → compare current vs baseline, flag anomalies
  5. correlate_with_deployments → check if regression started with a deploy
  6. analyze_regression         → deep dive on root cause + recommendations

Detection thresholds:
  p95 latency increase  > 10% vs baseline  → REGRESSION
  error rate increase   > 50% vs baseline  → REGRESSION
  Any metric           > 2× baseline       → CRITICAL_REGRESSION

CloudWatch namespaces used:
  AWS/ApplicationELB  — TargetResponseTime, HTTPCode_Target_5XX_Count, RequestCount
  AWS/ECS             — CPUUtilization, MemoryUtilization
  AWS/ApiGateway      — Latency, 5XXError, Count
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.agents.base import AgentResult, BaseAgent
from app.services.aws import AWSError, AWSService
from app.services.llm import LLMService

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

LATENCY_REGRESSION_PCT = 10.0    # p95 latency up >10% → regression
ERROR_RATE_REGRESSION_PCT = 50.0 # error rate up >50% → regression
CRITICAL_MULTIPLIER = 2.0        # any metric >2× baseline → critical


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class LatencySnapshot:
    service: str
    window_hours: float
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    sample_count: int
    raw_datapoints: list[dict]


@dataclass
class ErrorRateSnapshot:
    service: str
    window_hours: float
    total_requests: int
    total_errors: int
    error_rate_pct: float
    raw_datapoints: list[dict]


@dataclass
class RegressionResult:
    metric: str
    current_value: float
    baseline_value: float
    change_pct: float
    severity: str           # OK / WARNING / REGRESSION / CRITICAL_REGRESSION
    threshold_pct: float
    flagged: bool


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * pct / 100)
    return round(sorted_vals[min(idx, len(sorted_vals) - 1)], 2)


def _average(values: list[float]) -> float | None:
    return round(statistics.mean(values), 4) if values else None


def _pct_change(current: float, baseline: float) -> float:
    if baseline == 0:
        return 0.0
    return round((current - baseline) / baseline * 100, 2)


def _severity(change_pct: float, threshold_pct: float) -> tuple[str, bool]:
    if change_pct <= 0:
        return "OK", False
    if change_pct >= threshold_pct * CRITICAL_MULTIPLIER * 10:
        return "CRITICAL_REGRESSION", True
    if change_pct >= threshold_pct:
        return "REGRESSION", True
    if change_pct >= threshold_pct * 0.5:
        return "WARNING", False
    return "OK", False


def _fmt_latency(snap: LatencySnapshot) -> str:
    return (
        f"Latency [{snap.service}] last {snap.window_hours}h:\n"
        f"  p50={snap.p50_ms}ms  p95={snap.p95_ms}ms  p99={snap.p99_ms}ms\n"
        f"  samples={snap.sample_count}"
    )


def _fmt_error_rate(snap: ErrorRateSnapshot) -> str:
    return (
        f"Error rate [{snap.service}] last {snap.window_hours}h:\n"
        f"  requests={snap.total_requests}  errors={snap.total_errors}"
        f"  rate={snap.error_rate_pct}%"
    )


def _fmt_regression(r: RegressionResult) -> str:
    icon = {"OK": "✓", "WARNING": "⚡", "REGRESSION": "⚠", "CRITICAL_REGRESSION": "🚨"}.get(
        r.severity, "?"
    )
    direction = "▲" if r.change_pct > 0 else "▼"
    return (
        f"  {icon} {r.metric}: current={r.current_value:.2f}  "
        f"baseline={r.baseline_value:.2f}  "
        f"{direction}{abs(r.change_pct):.1f}%  [{r.severity}]"
    )


# ---------------------------------------------------------------------------
# CloudWatch helpers
# ---------------------------------------------------------------------------

def _fetch_metric_values(
    aws: AWSService,
    namespace: str,
    metric_name: str,
    dimensions: dict[str, str],
    hours: float,
    period_seconds: int = 60,
    stat: str = "Average",
) -> list[float]:
    """Fetch a metric and return a flat list of float values."""
    try:
        m = aws.get_metrics(
            namespace=namespace,
            metric_name=metric_name,
            dimensions=dimensions,
            minutes=int(hours * 60),
            period=period_seconds,
            statistics=[stat],
        )
        key = stat.lower()
        return [dp.get(key, dp.get("average", 0)) for dp in m.datapoints if dp.get(key, dp.get("average"))]
    except AWSError:
        return []


def _guess_namespace_and_dims(service: str) -> list[tuple[str, dict[str, str]]]:
    """Return candidate (namespace, dimensions) pairs for a service name."""
    return [
        ("AWS/ApplicationELB", {"LoadBalancer": service}),
        ("AWS/ApiGateway",     {"ApiName": service}),
        ("AWS/ECS",            {"ServiceName": service, "ClusterName": "default"}),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def get_latency_metrics(
    service: str,
    period_hours: float,
    aws: AWSService,
    namespace: str | None = None,
    dimensions: dict | None = None,
) -> str:
    """Fetch p50, p95, p99 latency for a service over the given window."""
    period_seconds = max(60, int(period_hours * 3600 / 100))  # ~100 datapoints

    candidates = (
        [(namespace, dimensions)] if namespace and dimensions
        else _guess_namespace_and_dims(service)
    )

    values_ms: list[float] = []
    used_ns = ""
    for ns, dims in candidates:
        metric = "TargetResponseTime" if "ELB" in ns else (
            "Latency" if "ApiGateway" in ns else "CPUUtilization"
        )
        raw = _fetch_metric_values(aws, ns, metric, dims, period_hours, period_seconds)
        if raw:
            # ELB/APIGW return seconds → convert to ms
            multiplier = 1000.0 if ns != "AWS/ECS" else 1.0
            values_ms = [v * multiplier for v in raw]
            used_ns = ns
            break

    snap = LatencySnapshot(
        service=service,
        window_hours=period_hours,
        p50_ms=_percentile(values_ms, 50),
        p95_ms=_percentile(values_ms, 95),
        p99_ms=_percentile(values_ms, 99),
        sample_count=len(values_ms),
        raw_datapoints=[],
    )

    if not values_ms:
        return (
            f"Latency [{service}] — no CloudWatch data found for the last {period_hours}h.\n"
            "Check that the service name matches an ALB, API Gateway, or ECS service."
        )

    return f"{_fmt_latency(snap)}\n  namespace={used_ns}"


async def get_error_rates(
    service: str,
    period_hours: float,
    aws: AWSService,
    namespace: str | None = None,
    dimensions: dict | None = None,
) -> str:
    """Fetch error percentage (5xx / total requests) over the given window."""
    period_seconds = max(60, int(period_hours * 3600 / 100))

    candidates = (
        [(namespace, dimensions)] if namespace and dimensions
        else _guess_namespace_and_dims(service)
    )

    errors: list[float] = []
    requests: list[float] = []
    used_ns = ""

    for ns, dims in candidates:
        err_metric = "HTTPCode_Target_5XX_Count" if "ELB" in ns else (
            "5XXError" if "ApiGateway" in ns else None
        )
        req_metric = "RequestCount" if "ELB" in ns else (
            "Count" if "ApiGateway" in ns else None
        )
        if not err_metric:
            continue

        e = _fetch_metric_values(aws, ns, err_metric, dims, period_hours, period_seconds, "Sum")
        r = _fetch_metric_values(aws, ns, req_metric, dims, period_hours, period_seconds, "Sum")
        if r:
            errors, requests, used_ns = e, r, ns
            break

    total_req = int(sum(requests))
    total_err = int(sum(errors))
    rate = round(total_err / total_req * 100, 4) if total_req else 0.0

    snap = ErrorRateSnapshot(
        service=service,
        window_hours=period_hours,
        total_requests=total_req,
        total_errors=total_err,
        error_rate_pct=rate,
        raw_datapoints=[],
    )

    if not requests:
        return (
            f"Error rate [{service}] — no CloudWatch data found for the last {period_hours}h.\n"
            "Check that the service name matches an ALB or API Gateway."
        )

    return f"{_fmt_error_rate(snap)}\n  namespace={used_ns}"


async def get_baseline(
    service: str,
    metric: str,
    aws: AWSService,
    days: int = 7,
    namespace: str | None = None,
    dimensions: dict | None = None,
) -> str:
    """Fetch a rolling N-day average as the performance baseline."""
    hours = days * 24
    period_seconds = 3600  # 1-hour granularity for long windows

    candidates = (
        [(namespace, dimensions)] if namespace and dimensions
        else _guess_namespace_and_dims(service)
    )

    values: list[float] = []
    used_ns = ""

    for ns, dims in candidates:
        raw = _fetch_metric_values(aws, ns, metric, dims, hours, period_seconds)
        if raw:
            values, used_ns = raw, ns
            break

    if not values:
        return (
            f"Baseline [{service}/{metric}] — no data for last {days} days.\n"
            "Cannot compute baseline without historical data."
        )

    avg = _average(values)
    p95 = _percentile(values, 95)

    return (
        f"Baseline [{service}/{metric}] last {days} days:\n"
        f"  average={avg}  p95={p95}  samples={len(values)}\n"
        f"  namespace={used_ns}"
    )


async def detect_regression(
    service: str,
    metric: str,
    aws: AWSService,
    threshold_percent: float | None = None,
    namespace: str | None = None,
    dimensions: dict | None = None,
) -> str:
    """
    Compare the last 1 hour vs the 7-day baseline.
    Flags regressions according to thresholds:
      - latency metrics: >10% increase
      - error metrics:   >50% increase
    """
    # Determine threshold from metric name if not explicit
    if threshold_percent is None:
        if any(k in metric.lower() for k in ("error", "5xx", "fault")):
            threshold_percent = ERROR_RATE_REGRESSION_PCT
        else:
            threshold_percent = LATENCY_REGRESSION_PCT

    candidates = (
        [(namespace, dimensions)] if namespace and dimensions
        else _guess_namespace_and_dims(service)
    )

    current_vals: list[float] = []
    baseline_vals: list[float] = []
    used_ns = ""

    for ns, dims in candidates:
        c = _fetch_metric_values(aws, ns, metric, dims, hours=1, period_seconds=60)
        b = _fetch_metric_values(aws, ns, metric, dims, hours=168, period_seconds=3600)
        if c and b:
            current_vals, baseline_vals, used_ns = c, b, ns
            break

    if not current_vals or not baseline_vals:
        return (
            f"Regression check [{service}/{metric}] — insufficient data.\n"
            "Need both current (1h) and baseline (7d) data."
        )

    current = _average(current_vals) or 0
    baseline = _average(baseline_vals) or 0
    change_pct = _pct_change(current, baseline)
    sev, flagged = _severity(change_pct, threshold_percent)

    result = RegressionResult(
        metric=metric,
        current_value=current,
        baseline_value=baseline,
        change_pct=change_pct,
        severity=sev,
        threshold_pct=threshold_percent,
        flagged=flagged,
    )

    lines = [
        f"Regression detection [{service}/{metric}]:",
        _fmt_regression(result),
        f"  threshold={threshold_percent}%  namespace={used_ns}",
    ]

    if flagged:
        lines.append(
            f"\n  ⚠ REGRESSION DETECTED: {metric} is {abs(change_pct):.1f}% "
            f"{'above' if change_pct > 0 else 'below'} the 7-day baseline."
        )

    return "\n".join(lines)


async def correlate_with_deployments(
    service: str,
    regression_time: str,
    aws: AWSService,
    cluster: str = "default",
    hours: int = 6,
) -> str:
    """Check whether a regression start time coincides with a recent deployment."""
    try:
        status = aws.get_ecs_status(cluster, service)
    except AWSError as exc:
        return f"Could not fetch ECS deployments: {exc}"

    try:
        reg_dt = datetime.fromisoformat(regression_time.replace("Z", "+00:00"))
    except ValueError:
        reg_dt = datetime.now(timezone.utc) - timedelta(hours=1)

    cutoff = reg_dt - timedelta(hours=hours)
    correlated = []

    for d in status.deployments:
        created_str = d.get("created_at", "")
        try:
            created = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            if cutoff <= created <= reg_dt + timedelta(hours=1):
                correlated.append((created, d))
        except (ValueError, AttributeError):
            pass

    if not correlated:
        return (
            f"No deployments found within {hours}h of regression time {regression_time}.\n"
            "Regression is likely NOT deployment-related."
        )

    lines = [
        f"⚠ DEPLOYMENT CORRELATION FOUND for {service}:",
        f"  Regression time: {regression_time}",
    ]
    for created, d in correlated:
        delta_min = int((reg_dt - created).total_seconds() / 60)
        lines.append(
            f"  Deploy [{d['id']}] at {created.strftime('%H:%M UTC')} "
            f"({delta_min}min before regression)  status={d['status']}"
        )
    lines.append("\nThis regression likely started with the above deployment.")
    return "\n".join(lines)


async def analyze_regression(
    service: str,
    metric_data: str,
    llm: LLMService,
) -> str:
    """Deep-dive LLM analysis of a detected regression — root cause + recommendations."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    prompt = f"""You are a senior SRE analyzing an application performance regression.

SERVICE: {service}
ANALYZED AT: {now}

METRIC DATA AND REGRESSION DETAILS:
{metric_data}

Provide a structured performance analysis in EXACTLY this format:

# Performance Regression Report: {service}

## Regression Summary
<1-2 sentences: which metric, how much it regressed, over what timeframe>

## Likely Root Causes
1. <most likely cause with reasoning>
2. <second most likely cause>
3. <third if applicable>

## Deployment Correlation
<YES / NO / POSSIBLE> — <explain connection or lack thereof>

## Recommended Actions

### Immediate (do now)
- [ ] <action> — <expected impact>

### Investigate
- [ ] <what to look at next to confirm root cause>

### Fix
- [ ] <concrete code/config change to resolve the regression>

## Metrics to Watch
<list 2-3 metrics to monitor to confirm recovery after fixing>

## Estimated Impact
<user-facing impact: latency increase users see, % of requests affected, etc.>

Be specific. Name actual metric values, not vague descriptions."""

    return await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system=(
            "You are a senior SRE performing performance regression analysis. "
            "Be precise, reference actual numbers from the data, and prioritize "
            "actionable recommendations over theory."
        ),
    )


# ---------------------------------------------------------------------------
# PerformanceAgent
# ---------------------------------------------------------------------------

class PerformanceAgent(BaseAgent):
    """
    Monitors application performance metrics, detects regressions against
    a rolling baseline, correlates with deployments, and recommends fixes.

    Usage:
        agent = PerformanceAgent()

        # Natural language
        result = await agent.run("Check performance of the api service")

        # Structured
        result = await agent.run(json.dumps({
            "service": "api",
            "cluster": "prod",
            "namespace": "AWS/ApplicationELB",
            "dimensions": {"LoadBalancer": "app/prod-alb/abc123"},
            "period_hours": 1,
        }))
        print(result.answer)

    Input JSON fields:
        service      - service/ALB/API Gateway name
        cluster      - ECS cluster for deployment correlation (default: "default")
        namespace    - CloudWatch namespace (optional, auto-detected if omitted)
        dimensions   - CloudWatch dimensions dict (optional, auto-detected if omitted)
        period_hours - hours of recent data to analyze (default: 1)
        baseline_days - days of history for baseline (default: 7)
    """

    def __init__(self, aws: AWSService | None = None) -> None:
        super().__init__()
        self._aws = aws or AWSService()
        self._register_tools()

    def _register_tools(self) -> None:
        aws = self._aws
        llm = self._llm

        # Accumulate all metric data for the final analysis call
        self._metric_data: list[str] = []

        async def _get_latency_metrics(
            service: str,
            period_hours: float = 1.0,
            namespace: str = "",
            dimensions: dict | None = None,
        ) -> str:
            out = await get_latency_metrics(
                service, period_hours, aws,
                namespace or None, dimensions or None,
            )
            self._metric_data.append(out)
            return out

        async def _get_error_rates(
            service: str,
            period_hours: float = 1.0,
            namespace: str = "",
            dimensions: dict | None = None,
        ) -> str:
            out = await get_error_rates(
                service, period_hours, aws,
                namespace or None, dimensions or None,
            )
            self._metric_data.append(out)
            return out

        async def _get_baseline(
            service: str,
            metric: str,
            days: int = 7,
            namespace: str = "",
            dimensions: dict | None = None,
        ) -> str:
            out = await get_baseline(
                service, metric, aws, days,
                namespace or None, dimensions or None,
            )
            self._metric_data.append(out)
            return out

        async def _detect_regression(
            service: str,
            metric: str,
            threshold_percent: float | None = None,
            namespace: str = "",
            dimensions: dict | None = None,
        ) -> str:
            out = await detect_regression(
                service, metric, aws, threshold_percent,
                namespace or None, dimensions or None,
            )
            self._metric_data.append(out)
            return out

        async def _correlate_with_deployments(
            service: str,
            regression_time: str = "",
            cluster: str = "default",
            hours: int = 6,
        ) -> str:
            t = regression_time or datetime.now(timezone.utc).isoformat()
            out = await correlate_with_deployments(service, t, aws, cluster, hours)
            self._metric_data.append(out)
            return out

        async def _analyze_regression(
            service: str,
            extra_context: str = "",
        ) -> str:
            all_data = "\n\n---\n\n".join(self._metric_data)
            if extra_context:
                all_data += f"\n\n---\n\nAdditional context:\n{extra_context}"
            return await analyze_regression(service, all_data, llm)

        self.register_tool(
            "get_latency_metrics",
            _get_latency_metrics,
            (
                "Fetch p50, p95, p99 latency for a service over a recent time window. "
                "Auto-detects ALB, API Gateway, or ECS CloudWatch namespace. "
                "Input: {service: string, period_hours: float (default 1), "
                "namespace: string (optional), dimensions: dict (optional)}"
            ),
        )
        self.register_tool(
            "get_error_rates",
            _get_error_rates,
            (
                "Fetch error rate (5xx / total requests) for a service. "
                "Input: {service: string, period_hours: float (default 1), "
                "namespace: string (optional), dimensions: dict (optional)}"
            ),
        )
        self.register_tool(
            "get_baseline",
            _get_baseline,
            (
                "Fetch the rolling N-day average for a metric as a performance baseline. "
                "Use before detect_regression to understand normal behaviour. "
                "Input: {service: string, metric: string, days: int (default 7), "
                "namespace: string (optional), dimensions: dict (optional)}"
            ),
        )
        self.register_tool(
            "detect_regression",
            _detect_regression,
            (
                "Compare the last 1 hour vs the 7-day baseline for a metric. "
                "Flags REGRESSION if p95 latency increases >10% or error rate increases >50%. "
                "Flags CRITICAL_REGRESSION if the metric exceeds 2× the baseline. "
                "Input: {service: string, metric: string, threshold_percent: float (optional), "
                "namespace: string (optional), dimensions: dict (optional)}"
            ),
        )
        self.register_tool(
            "correlate_with_deployments",
            _correlate_with_deployments,
            (
                "Check if a regression start time coincides with a recent ECS deployment. "
                "Pass the approximate time the regression started (ISO 8601 or 'now'). "
                "Input: {service: string, regression_time: string (ISO 8601, default now), "
                "cluster: string (default 'default'), hours: int (default 6)}"
            ),
        )
        self.register_tool(
            "analyze_regression",
            _analyze_regression,
            (
                "Deep-dive LLM analysis of all collected metric data. "
                "Call this LAST after gathering metrics, baseline, and deployment correlation. "
                "Returns: root causes, deployment correlation, recommended actions, "
                "metrics to watch, and estimated user impact. "
                "Input: {service: string, extra_context: string (optional)}"
            ),
        )

    async def run(self, user_input: str) -> AgentResult:
        """
        Run the performance agent.

        Accepts JSON:
            {
              "service": "api",
              "cluster": "prod",
              "namespace": "AWS/ApplicationELB",
              "dimensions": {"LoadBalancer": "app/prod-alb/abc123"},
              "period_hours": 1,
              "baseline_days": 7
            }
        or plain text:
            "Check performance of the api service"
        """
        self._metric_data = []  # reset accumulator

        try:
            params = json.loads(user_input)
            service = params.get("service", "unknown")
            cluster = params.get("cluster", "default")
            namespace = params.get("namespace", "")
            dimensions = params.get("dimensions", {})
            period_hours = params.get("period_hours", 1)
            baseline_days = params.get("baseline_days", 7)

            ns_hint = f" namespace={namespace}" if namespace else ""
            dim_hint = f" dimensions={dimensions}" if dimensions else ""

            prompt = (
                f"Analyze performance for service '{service}' (cluster={cluster}){ns_hint}{dim_hint}.\n\n"
                "Follow these steps:\n"
                f"1. get_latency_metrics for '{service}' (period_hours={period_hours})\n"
                f"2. get_error_rates for '{service}' (period_hours={period_hours})\n"
                f"3. get_baseline for key latency and error metrics (days={baseline_days})\n"
                "4. detect_regression for p95 latency (threshold=10%) and error rate (threshold=50%)\n"
                f"5. correlate_with_deployments for '{service}' in cluster '{cluster}'\n"
                f"6. analyze_regression for '{service}' with all collected data as your final answer\n\n"
                "Flag any metric where:\n"
                "  - p95 latency increased >10% vs baseline\n"
                "  - Error rate increased >50% vs baseline\n"
                "  - Any metric exceeds 2× baseline (CRITICAL)\n"
                "  - Regression correlates with a recent deployment"
            )
        except (json.JSONDecodeError, KeyError):
            service = "the service"
            prompt = (
                f"{user_input}\n\n"
                "Analyze performance: fetch latency metrics, error rates, compare against "
                "the 7-day baseline, detect regressions (latency >10%, errors >50%), "
                "correlate with recent deployments, then analyze_regression as your final answer."
            )

        return await super().run(prompt)
