"""
Alerting service — routes agent platform alerts to Slack and/or console.

Supported channels:
  Slack   — POST to SLACK_WEBHOOK_URL (Block Kit payload with colour coding)
  Console — fallback print when Slack is not configured or the POST fails

Alert conditions checked by AlertingService.check_*():
  check_agent_error_rate(agent, error_pct)
      → ERROR  if error_pct > settings.alert_error_rate_pct   (default 10 %)

  check_agent_latency(agent, p95_sec)
      → WARNING if p95_sec > settings.alert_latency_p95_sec   (default 30 s)

  check_approval_pending(request_id, pending_min)
      → WARNING if pending_min > settings.alert_approval_pending_min (default 60 min)

  check_daily_cost(spent_usd, budget_usd)
      → WARNING  if spent >= 80 % of budget
      → CRITICAL if spent >= 100 % of budget

  check_provider_error(exc)
      → CRITICAL immediately, no threshold, on any LLM-provider auth/billing
        failure (401, or a 400 naming a low credit balance). These are
        binary "nothing will work until this is fixed" signals — waiting
        for an error-rate percentage to accumulate is the wrong shape of
        check for them.

Until Aug 2026, check_agent_error_rate() and check_daily_cost() above were
fully implemented but never called from anywhere — a real ~21.5h outage
(an invalid Anthropic API key tripped the 'anthropic_llm' circuit breaker
and every Triage/DiagnosisAgent call was rejected for most of a day) went
completely unnoticed as a result. run_forever() below is what actually
calls them now, on a fixed interval; wire it up at app startup like the
other background loops (drift_detector, threshold_monitor, ...).

Usage:
    from app.services.alerting import alerting_service

    await alerting_service.check_agent_error_rate("IncidentResponseAgent", 15.0)
    await alerting_service.check_daily_cost(8.50, 10.00)
    asyncio.create_task(alerting_service.run_forever())   # periodic health check
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# How often run_forever() re-checks per-agent error rates and daily cost.
# Matches the cadence of the other periodic health loops in this codebase
# (threshold_monitor, drift_detector) rather than inventing a new interval.
_HEALTH_CHECK_INTERVAL_SECONDS = 300


# ---------------------------------------------------------------------------
# Severity
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    INFO     = "info"
    WARNING  = "warning"
    ERROR    = "error"
    CRITICAL = "critical"

    @property
    def emoji(self) -> str:
        return {
            "info":     "ℹ️",
            "warning":  "⚠️",
            "error":    "🔴",
            "critical": "🚨",
        }[self.value]

    @property
    def color(self) -> str:
        """Slack attachment sidebar colour."""
        return {
            "info":     "#36a64f",   # green
            "warning":  "#ffaa00",   # amber
            "error":    "#e01e5a",   # red
            "critical": "#7b0000",   # dark red
        }[self.value]


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------

@dataclass
class Alert:
    severity: Severity
    title: str
    message: str
    source: str                           # e.g. "IncidentResponseAgent" or "AlertingService"
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M UTC")
        return (
            f"[{self.severity.value.upper()}] {self.title} | "
            f"source={self.source} | {ts}\n  {self.message}"
        )


# ---------------------------------------------------------------------------
# AlertingService
# ---------------------------------------------------------------------------

class AlertingService:
    """
    Routes Alert objects to the appropriate notification channel.

    Channel selection:
      - Slack  → when settings.slack_webhook_url is set
      - Console → always (as fallback or when Slack is absent)

    Slack channel can be overridden per call via the `channel` parameter.
    When no channel is specified, the severity determines the channel:
      critical/error  → #incidents  (override with alert_slack_incidents_channel)
      warning/info    → #monitoring (override with alert_slack_monitoring_channel)
    """

    _INCIDENTS_CHANNEL  = "#incidents"
    _MONITORING_CHANNEL = "#monitoring"

    def __init__(self) -> None:
        self._http: httpx.AsyncClient | None = None
        self._running = False

    async def _client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=10.0)
        return self._http

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_alert(self, alert: Alert, channel: str | None = None) -> None:
        """Route alert to Slack (if configured) and always echo to console."""
        self._send_console(alert)

        if settings.slack_webhook_url:
            target = channel or (
                self._INCIDENTS_CHANNEL
                if alert.severity in (Severity.CRITICAL, Severity.ERROR)
                else self._MONITORING_CHANNEL
            )
            await self._send_slack(alert, target)

    # ------------------------------------------------------------------
    # Alert condition checks
    # ------------------------------------------------------------------

    async def check_agent_error_rate(self, agent_name: str, error_pct: float) -> None:
        """Fire an ERROR alert when an agent's error rate exceeds the threshold."""
        threshold = settings.alert_error_rate_pct
        if error_pct <= threshold:
            return
        await self.send_alert(Alert(
            severity=Severity.ERROR,
            title=f"High error rate: {agent_name}",
            message=(
                f"{agent_name} error rate is {error_pct:.1f}% "
                f"(threshold: {threshold:.0f}%). "
                "Check agent logs and recent tool failures."
            ),
            source="AlertingService",
            metadata={"agent": agent_name, "error_pct": error_pct, "threshold_pct": threshold},
        ))

    async def check_agent_latency(self, agent_name: str, p95_sec: float) -> None:
        """Fire a WARNING alert when an agent's p95 latency exceeds the threshold."""
        threshold = settings.alert_latency_p95_sec
        if p95_sec <= threshold:
            return
        await self.send_alert(Alert(
            severity=Severity.WARNING,
            title=f"High latency: {agent_name}",
            message=(
                f"{agent_name} p95 latency is {p95_sec:.1f}s "
                f"(threshold: {threshold:.0f}s). "
                "Consider reducing MAX_ITERATIONS or tool timeout limits."
            ),
            source="AlertingService",
            metadata={"agent": agent_name, "p95_sec": p95_sec, "threshold_sec": threshold},
        ))

    async def check_approval_pending(self, request_id: str, pending_min: float) -> None:
        """Fire a WARNING alert when an approval request has been waiting too long."""
        threshold = settings.alert_approval_pending_min
        if pending_min <= threshold:
            return
        await self.send_alert(Alert(
            severity=Severity.WARNING,
            title="Approval request stale",
            message=(
                f"Approval request {request_id} has been PENDING for "
                f"{pending_min:.0f} minutes (threshold: {threshold} min). "
                f"Approve or reject at: POST /approvals/{request_id}/approve"
            ),
            source="AlertingService",
            metadata={"request_id": request_id, "pending_min": pending_min},
        ))

    async def check_daily_cost(
        self,
        spent_usd: float,
        budget_usd: float | None = None,
    ) -> None:
        """
        Fire a WARNING at 80 % of budget and CRITICAL at 100 %.
        budget_usd defaults to settings.alert_daily_budget_usd.
        """
        budget = budget_usd if budget_usd is not None else settings.alert_daily_budget_usd
        if budget <= 0:
            return

        pct = spent_usd / budget * 100

        if pct >= settings.alert_budget_critical_pct:
            await self.send_alert(Alert(
                severity=Severity.CRITICAL,
                title="Daily LLM budget exceeded",
                message=(
                    f"Daily spend is ${spent_usd:.2f} / ${budget:.2f} "
                    f"({pct:.0f}% of budget). "
                    "Agent calls may be throttled. Review usage immediately."
                ),
                source="AlertingService",
                metadata={"spent_usd": spent_usd, "budget_usd": budget, "pct": pct},
            ))
        elif pct >= settings.alert_budget_warning_pct:
            await self.send_alert(Alert(
                severity=Severity.WARNING,
                title="Daily LLM budget at 80 %",
                message=(
                    f"Daily spend is ${spent_usd:.2f} / ${budget:.2f} "
                    f"({pct:.0f}% of budget). "
                    "Approaching limit — consider reducing non-critical agent calls."
                ),
                source="AlertingService",
                metadata={"spent_usd": spent_usd, "budget_usd": budget, "pct": pct},
            ))

    async def check_provider_error(self, exc: BaseException) -> None:
        """
        Fire an immediate CRITICAL alert on an LLM-provider auth/billing
        failure — no threshold, no window, first occurrence fires.

        These fail every call (retries won't help, `_is_retryable` already
        excludes them) and are exactly the failure class that caused a real
        ~21.5h silent outage before this method existed: an invalid API key
        produced 401s, which tripped the circuit breaker, which then
        rejected every call for the rest of the window — all without a
        single alert firing, because nothing was watching for the *cause*,
        only (unwired) aggregate error rates.

        Duck-types `status_code` off the exception rather than importing
        anthropic/litellm exception classes here, since this is called from
        both the raw-SDK path (app/services/llm.py) and the LiteLLM gateway
        path (app/services/llm_gateway.py), which raise different exception
        hierarchies for the same underlying HTTP status.
        """
        status_code = getattr(exc, "status_code", None)
        message = str(exc)
        is_auth_error = status_code == 401
        is_billing_error = status_code == 400 and "credit balance" in message.lower()
        if not (is_auth_error or is_billing_error):
            return

        kind = "authentication" if is_auth_error else "billing"
        await self.send_alert(Alert(
            severity=Severity.CRITICAL,
            title=f"LLM provider {kind} failure",
            message=(
                f"An LLM call failed with a {kind} error ({status_code}) that will "
                f"never succeed on retry: {message[:300]}. Every agent call will "
                "keep failing until this is fixed — check API key validity / "
                "account credit balance immediately."
            ),
            source="AlertingService",
            metadata={"status_code": status_code, "kind": kind, "raw_error": message[:500]},
        ))

    async def check_agent_health(self) -> None:
        """
        Pull today's per-agent error rates (agent_tracker) and today's total
        LLM spend (the gateway's cost tracker) and run them through the
        checks above. This is what actually calls check_agent_error_rate()/
        check_daily_cost() now — see the module docstring for why that
        matters.
        """
        from app.services.agent_tracker import agent_tracker
        from app.services.llm_gateway import llm_gateway

        for stat in agent_tracker.stats():
            if stat["runs_today"] > 0:
                await self.check_agent_error_rate(stat["agent_name"], stat["error_rate"] * 100)

        costs = llm_gateway.costs_today()
        await self.check_daily_cost(costs["llm_costs_today_usd"])

    async def run_forever(self) -> None:
        """Background loop: re-run check_agent_health() every _HEALTH_CHECK_INTERVAL_SECONDS."""
        self._running = True
        logger.info(
            "[AlertingService] Health-check loop started (interval %ds)",
            _HEALTH_CHECK_INTERVAL_SECONDS,
        )
        while self._running:
            try:
                await self.check_agent_health()
            except Exception as exc:
                logger.error("[AlertingService] Health check failed: %s", exc)
            await asyncio.sleep(_HEALTH_CHECK_INTERVAL_SECONDS)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Channels
    # ------------------------------------------------------------------

    def _send_console(self, alert: Alert) -> None:
        """Print alert to stdout (always active — useful for local dev and as fallback)."""
        line = (
            f"{alert.severity.emoji}  {alert}"
        )
        if alert.severity in (Severity.CRITICAL, Severity.ERROR):
            logger.error(line)
        elif alert.severity == Severity.WARNING:
            logger.warning(line)
        else:
            logger.info(line)
        print(line)

    async def _send_slack(self, alert: Alert, channel: str) -> None:
        """POST a Block Kit message to the Slack incoming webhook."""
        ts = alert.timestamp.strftime("%Y-%m-%d %H:%M UTC")
        payload = {
            "channel": channel,
            "attachments": [
                {
                    "color": alert.severity.color,
                    "blocks": [
                        {
                            "type": "header",
                            "text": {
                                "type": "plain_text",
                                "text": f"{alert.severity.emoji}  {alert.title}",
                                "emoji": True,
                            },
                        },
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": alert.message},
                        },
                        {
                            "type": "context",
                            "elements": [
                                {
                                    "type": "mrkdwn",
                                    "text": (
                                        f"*Source:* {alert.source}  |  "
                                        f"*Severity:* {alert.severity.value.upper()}  |  "
                                        f"*Time:* {ts}"
                                    ),
                                }
                            ],
                        },
                    ],
                }
            ],
        }

        try:
            client = await self._client()
            resp = await client.post(settings.slack_webhook_url, json=payload)
            resp.raise_for_status()
            logger.debug("[Alerting] Slack alert sent → %s (%s)", channel, alert.title)
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "[Alerting] Slack POST failed (HTTP %s): %s — alert was logged to console only.",
                exc.response.status_code,
                alert.title,
            )
        except Exception as exc:
            logger.warning(
                "[Alerting] Slack POST error: %s — alert was logged to console only.", exc
            )


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

alerting_service = AlertingService()
