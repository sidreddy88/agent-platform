"""
Tests for AlertingService.

All Slack HTTP calls are mocked — no network required.

Run:
    pytest tests/test_alerting.py -v
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.alerting import Alert, AlertingService, Severity


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _service(slack_url: str = "") -> AlertingService:
    svc = AlertingService()
    with patch("app.core.config.settings") as mock_settings:
        mock_settings.slack_webhook_url         = slack_url
        mock_settings.alert_error_rate_pct      = 10.0
        mock_settings.alert_latency_p95_sec     = 30.0
        mock_settings.alert_approval_pending_min = 60
        mock_settings.alert_budget_warning_pct  = 80.0
        mock_settings.alert_budget_critical_pct = 100.0
        mock_settings.alert_daily_budget_usd    = 10.0
    return svc


def _alert(severity: Severity = Severity.WARNING, source: str = "test") -> Alert:
    return Alert(
        severity=severity,
        title="Test alert",
        message="Something happened.",
        source=source,
        timestamp=datetime(2024, 1, 15, 10, 0, 0, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Alert model
# ---------------------------------------------------------------------------

class TestAlertModel:
    def test_str_contains_severity_title_source(self):
        a = _alert(Severity.ERROR)
        s = str(a)
        assert "ERROR" in s
        assert "Test alert" in s
        assert "test" in s

    def test_default_timestamp_is_utc(self):
        a = Alert(severity=Severity.INFO, title="t", message="m", source="s")
        assert a.timestamp.tzinfo is not None

    def test_metadata_defaults_to_empty_dict(self):
        a = Alert(severity=Severity.INFO, title="t", message="m", source="s")
        assert a.metadata == {}


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

class TestSeverity:
    @pytest.mark.parametrize("sev,expected_emoji", [
        (Severity.INFO,     "ℹ️"),
        (Severity.WARNING,  "⚠️"),
        (Severity.ERROR,    "🔴"),
        (Severity.CRITICAL, "🚨"),
    ])
    def test_emoji(self, sev, expected_emoji):
        assert sev.emoji == expected_emoji

    @pytest.mark.parametrize("sev", list(Severity))
    def test_color_is_hex(self, sev):
        assert sev.color.startswith("#")


# ---------------------------------------------------------------------------
# Console channel
# ---------------------------------------------------------------------------

class TestConsoleChannel:
    def test_console_prints_alert(self, capsys):
        svc = AlertingService()
        svc._send_console(_alert(Severity.WARNING))
        out = capsys.readouterr().out
        assert "Test alert" in out

    def test_console_includes_severity_emoji(self, capsys):
        svc = AlertingService()
        svc._send_console(_alert(Severity.CRITICAL))
        out = capsys.readouterr().out
        assert "🚨" in out


# ---------------------------------------------------------------------------
# Slack channel
# ---------------------------------------------------------------------------

class TestSlackChannel:
    def _mock_http_client(self, side_effect=None):
        """Return an AsyncMock http client that won't be replaced by _client()."""
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.is_closed = False   # prevent _client() from creating a new real client
        mock_client.post = AsyncMock(
            return_value=mock_resp if side_effect is None else None,
            side_effect=side_effect,
        )
        return mock_client

    @pytest.mark.asyncio
    async def test_slack_posts_to_correct_channel(self):
        svc = AlertingService()
        mock_client = self._mock_http_client()
        svc._http = mock_client

        with patch("app.services.alerting.settings") as cfg:
            cfg.slack_webhook_url = "https://hooks.slack.com/test"
            await svc._send_slack(_alert(Severity.ERROR), "#incidents")

        mock_client.post.assert_called_once()
        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["channel"] == "#incidents"

    @pytest.mark.asyncio
    async def test_slack_payload_contains_title_and_message(self):
        svc = AlertingService()
        mock_client = self._mock_http_client()
        svc._http = mock_client

        with patch("app.services.alerting.settings") as cfg:
            cfg.slack_webhook_url = "https://hooks.slack.com/test"
            await svc._send_slack(_alert(Severity.WARNING), "#monitoring")

        payload = mock_client.post.call_args.kwargs["json"]
        full_text = str(payload["attachments"][0])
        assert "Test alert" in full_text
        assert "Something happened" in full_text

    @pytest.mark.asyncio
    async def test_slack_uses_correct_color_per_severity(self):
        svc = AlertingService()
        mock_client = self._mock_http_client()
        svc._http = mock_client

        with patch("app.services.alerting.settings") as cfg:
            cfg.slack_webhook_url = "https://hooks.slack.com/test"
            await svc._send_slack(_alert(Severity.CRITICAL), "#incidents")

        payload = mock_client.post.call_args.kwargs["json"]
        assert payload["attachments"][0]["color"] == Severity.CRITICAL.color

    @pytest.mark.asyncio
    async def test_slack_failure_does_not_raise(self):
        """A failed Slack POST is swallowed — the service never crashes on send."""
        import httpx

        mock_request  = MagicMock()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_client = self._mock_http_client(
            side_effect=httpx.HTTPStatusError(
                "server error", request=mock_request, response=mock_response
            )
        )
        svc = AlertingService()
        svc._http = mock_client

        with patch("app.services.alerting.settings") as cfg:
            cfg.slack_webhook_url = "https://hooks.slack.com/test"
            await svc._send_slack(_alert(), "#monitoring")  # must not raise


# ---------------------------------------------------------------------------
# send_alert routing
# ---------------------------------------------------------------------------

class TestSendAlertRouting:
    @pytest.mark.asyncio
    async def test_critical_routes_to_incidents_channel(self):
        svc = AlertingService()
        svc._send_slack = AsyncMock()
        svc._send_console = MagicMock()

        with patch("app.services.alerting.settings") as cfg:
            cfg.slack_webhook_url = "https://hooks.slack.com/test"
            await svc.send_alert(_alert(Severity.CRITICAL))

        svc._send_slack.assert_called_once()
        channel = svc._send_slack.call_args.args[1]
        assert channel == "#incidents"

    @pytest.mark.asyncio
    async def test_warning_routes_to_monitoring_channel(self):
        svc = AlertingService()
        svc._send_slack = AsyncMock()
        svc._send_console = MagicMock()

        with patch("app.services.alerting.settings") as cfg:
            cfg.slack_webhook_url = "https://hooks.slack.com/test"
            await svc.send_alert(_alert(Severity.WARNING))

        channel = svc._send_slack.call_args.args[1]
        assert channel == "#monitoring"

    @pytest.mark.asyncio
    async def test_channel_override_is_respected(self):
        svc = AlertingService()
        svc._send_slack = AsyncMock()
        svc._send_console = MagicMock()

        with patch("app.services.alerting.settings") as cfg:
            cfg.slack_webhook_url = "https://hooks.slack.com/test"
            await svc.send_alert(_alert(Severity.ERROR), channel="#custom-channel")

        channel = svc._send_slack.call_args.args[1]
        assert channel == "#custom-channel"

    @pytest.mark.asyncio
    async def test_no_slack_url_skips_slack(self):
        svc = AlertingService()
        svc._send_slack = AsyncMock()
        svc._send_console = MagicMock()

        with patch("app.services.alerting.settings") as cfg:
            cfg.slack_webhook_url = ""
            await svc.send_alert(_alert())

        svc._send_slack.assert_not_called()
        svc._send_console.assert_called_once()

    @pytest.mark.asyncio
    async def test_console_always_called(self):
        svc = AlertingService()
        svc._send_slack = AsyncMock()
        svc._send_console = MagicMock()

        with patch("app.services.alerting.settings") as cfg:
            cfg.slack_webhook_url = "https://hooks.slack.com/test"
            await svc.send_alert(_alert())

        svc._send_console.assert_called_once()


# ---------------------------------------------------------------------------
# Alert condition: agent error rate
# ---------------------------------------------------------------------------

class TestCheckAgentErrorRate:
    @pytest.mark.asyncio
    async def test_below_threshold_no_alert(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_error_rate_pct = 10.0
            await svc.check_agent_error_rate("MyAgent", 9.9)
        svc.send_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_at_threshold_no_alert(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_error_rate_pct = 10.0
            await svc.check_agent_error_rate("MyAgent", 10.0)
        svc.send_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_above_threshold_fires_error_alert(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_error_rate_pct = 10.0
            await svc.check_agent_error_rate("MyAgent", 15.0)
        svc.send_alert.assert_called_once()
        alert = svc.send_alert.call_args.args[0]
        assert alert.severity == Severity.ERROR
        assert "MyAgent" in alert.title
        assert "15.0" in alert.message

    @pytest.mark.asyncio
    async def test_alert_metadata_contains_agent_and_rate(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_error_rate_pct = 10.0
            await svc.check_agent_error_rate("CodeReviewAgent", 22.5)
        alert = svc.send_alert.call_args.args[0]
        assert alert.metadata["agent"] == "CodeReviewAgent"
        assert alert.metadata["error_pct"] == 22.5


# ---------------------------------------------------------------------------
# Alert condition: agent latency
# ---------------------------------------------------------------------------

class TestCheckAgentLatency:
    @pytest.mark.asyncio
    async def test_below_threshold_no_alert(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_latency_p95_sec = 30.0
            await svc.check_agent_latency("MyAgent", 29.9)
        svc.send_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_above_threshold_fires_warning(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_latency_p95_sec = 30.0
            await svc.check_agent_latency("MyAgent", 45.0)
        alert = svc.send_alert.call_args.args[0]
        assert alert.severity == Severity.WARNING
        assert "45.0" in alert.message

    @pytest.mark.asyncio
    async def test_alert_names_agent_in_title(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_latency_p95_sec = 30.0
            await svc.check_agent_latency("PerformanceAgent", 60.0)
        alert = svc.send_alert.call_args.args[0]
        assert "PerformanceAgent" in alert.title


# ---------------------------------------------------------------------------
# Alert condition: approval pending
# ---------------------------------------------------------------------------

class TestCheckApprovalPending:
    @pytest.mark.asyncio
    async def test_below_threshold_no_alert(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_approval_pending_min = 60
            await svc.check_approval_pending("REQ-001", 59)
        svc.send_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_above_threshold_fires_warning(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_approval_pending_min = 60
            await svc.check_approval_pending("REQ-001", 90)
        alert = svc.send_alert.call_args.args[0]
        assert alert.severity == Severity.WARNING
        assert "REQ-001" in alert.message

    @pytest.mark.asyncio
    async def test_alert_includes_approve_url(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_approval_pending_min = 60
            await svc.check_approval_pending("REQ-042", 120)
        alert = svc.send_alert.call_args.args[0]
        assert "/approvals/REQ-042/approve" in alert.message


# ---------------------------------------------------------------------------
# Alert condition: daily cost
# ---------------------------------------------------------------------------

class TestCheckDailyCost:
    @pytest.mark.asyncio
    async def test_below_80pct_no_alert(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_budget_warning_pct  = 80.0
            cfg.alert_budget_critical_pct = 100.0
            cfg.alert_daily_budget_usd    = 10.0
            await svc.check_daily_cost(7.99, budget_usd=10.0)
        svc.send_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_at_80pct_fires_warning(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_budget_warning_pct  = 80.0
            cfg.alert_budget_critical_pct = 100.0
            cfg.alert_daily_budget_usd    = 10.0
            await svc.check_daily_cost(8.00, budget_usd=10.0)
        alert = svc.send_alert.call_args.args[0]
        assert alert.severity == Severity.WARNING
        assert "$8.00" in alert.message

    @pytest.mark.asyncio
    async def test_at_100pct_fires_critical(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_budget_warning_pct  = 80.0
            cfg.alert_budget_critical_pct = 100.0
            cfg.alert_daily_budget_usd    = 10.0
            await svc.check_daily_cost(10.00, budget_usd=10.0)
        alert = svc.send_alert.call_args.args[0]
        assert alert.severity == Severity.CRITICAL

    @pytest.mark.asyncio
    async def test_over_budget_fires_critical(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_budget_warning_pct  = 80.0
            cfg.alert_budget_critical_pct = 100.0
            cfg.alert_daily_budget_usd    = 10.0
            await svc.check_daily_cost(12.50, budget_usd=10.0)
        alert = svc.send_alert.call_args.args[0]
        assert alert.severity == Severity.CRITICAL
        assert "$12.50" in alert.message

    @pytest.mark.asyncio
    async def test_uses_settings_budget_when_not_passed(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_budget_warning_pct  = 80.0
            cfg.alert_budget_critical_pct = 100.0
            cfg.alert_daily_budget_usd    = 10.0
            await svc.check_daily_cost(9.0)   # no budget_usd arg → uses settings
        alert = svc.send_alert.call_args.args[0]
        assert alert.severity == Severity.WARNING

    @pytest.mark.asyncio
    async def test_zero_budget_skips_alert(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_budget_warning_pct  = 80.0
            cfg.alert_budget_critical_pct = 100.0
            cfg.alert_daily_budget_usd    = 0.0
            await svc.check_daily_cost(5.0, budget_usd=0.0)
        svc.send_alert.assert_not_called()

    @pytest.mark.asyncio
    async def test_alert_metadata_contains_spend_and_budget(self):
        svc = AlertingService()
        svc.send_alert = AsyncMock()
        with patch("app.services.alerting.settings") as cfg:
            cfg.alert_budget_warning_pct  = 80.0
            cfg.alert_budget_critical_pct = 100.0
            cfg.alert_daily_budget_usd    = 10.0
            await svc.check_daily_cost(8.50, budget_usd=10.0)
        alert = svc.send_alert.call_args.args[0]
        assert alert.metadata["spent_usd"] == 8.50
        assert alert.metadata["budget_usd"] == 10.0
