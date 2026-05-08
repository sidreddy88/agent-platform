"""
Tests for POST /webhooks/cloudwatch-alarm — push-based ingest from
CloudWatch alarms via SNS. Drives system MTTD from "human attention lag"
to single-digit minutes without polling production.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.webhooks import _alarm_payload_to_event, router


@pytest.fixture
def client() -> TestClient:
    """Fresh app + client; each test gets an empty PendingEventStore.

    The route handler in webhooks.py binds to the module-level
    `pending_event_store` singleton at import time, so resetting the
    singleton's internal state directly is the only way to isolate tests.
    """
    from app.services.pending_events import pending_event_store
    pending_event_store.clear()
    pending_event_store.reset_dismissed()

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Notification path — alarm transitions become ErrorEvents
# ---------------------------------------------------------------------------

def _alarm_notification(alarm_name: str = "auto-svc-errors", state: str = "ALARM") -> dict:
    """Build a minimal SNS Notification envelope wrapping a CloudWatch alarm."""
    alarm_body = {
        "AlarmName": alarm_name,
        "AlarmDescription": "Auto-generated alarm",
        "AWSAccountId": "123456789012",
        "NewStateValue": state,
        "NewStateReason": "Threshold Crossed: 1 datapoint above 1.0",
        "StateChangeTime": "2026-05-07T12:34:56.789+0000",
        "Region": "us-east-1",
        "AlarmArn": f"arn:aws:cloudwatch:us-east-1:123456789012:alarm:{alarm_name}",
        "OldStateValue": "OK",
        "Trigger": {
            "MetricName": "Errors",
            "Namespace": "AWS/Lambda",
            "Statistic": "Sum",
            "Dimensions": [{"name": "ServiceName", "value": "allinterviews"}],
            "Period": 300,
            "EvaluationPeriods": 1,
            "Threshold": 1.0,
        },
    }
    return {
        "Type": "Notification",
        "MessageId": "test-message-id",
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:agent-platform-alarms",
        "Subject": f"ALARM: {alarm_name}",
        "Message": json.dumps(alarm_body),
        "Timestamp": "2026-05-07T12:34:57.000Z",
    }


def test_notification_creates_pending_event(client):
    resp = client.post("/webhooks/cloudwatch-alarm", json=_alarm_notification())
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert data["status"] == "queued"
    assert data["is_new"] is True
    assert data["occurrences"] == 1
    assert "event_id" in data


def test_notification_dedups_repeat_alarm(client):
    payload = _alarm_notification()
    first = client.post("/webhooks/cloudwatch-alarm", json=payload)
    second = client.post("/webhooks/cloudwatch-alarm", json=payload)

    assert first.json()["is_new"] is True
    assert second.json()["is_new"] is False
    assert second.json()["occurrences"] == 2


def test_non_alarm_transitions_are_ignored(client):
    """OK and INSUFFICIENT_DATA shouldn't open new incidents."""
    for state in ("OK", "INSUFFICIENT_DATA"):
        resp = client.post(
            "/webhooks/cloudwatch-alarm",
            json=_alarm_notification(state=state),
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ignored_non_alarm"
        assert resp.json()["state"] == state


def test_alarm_payload_to_event_extracts_dimensions():
    """Alarm dimensions surface as the ErrorEvent.service field."""
    alarm = {
        "AlarmName": "auto-payments-5xx",
        "NewStateValue": "ALARM",
        "Trigger": {
            "MetricName": "5XXError",
            "Namespace": "AWS/ApiGateway",
            "Dimensions": [{"name": "ServiceName", "value": "payments"}],
        },
        "AlarmArn": "arn:aws:cloudwatch:us-east-1:123:alarm:auto-payments-5xx",
        "Region": "us-east-1",
    }
    event = _alarm_payload_to_event(alarm)
    assert event.service == "payments"
    assert event.error_type == "CW_ALARM_AWS_APIGATEWAY_5XXERROR"
    assert event.metadata["alarm_arn"].endswith("auto-payments-5xx")
    assert event.metadata["new_state"] == "ALARM"


def test_alarm_payload_handles_capitalised_dimension_keys():
    """SNS sometimes uses Capitalised Name/Value; both must work."""
    alarm = {
        "AlarmName": "x",
        "NewStateValue": "ALARM",
        "Trigger": {
            "MetricName": "Errors",
            "Namespace": "AWS/Lambda",
            "Dimensions": [{"Name": "FunctionName", "Value": "image-processor"}],
        },
    }
    event = _alarm_payload_to_event(alarm)
    assert event.service == "image-processor"


# ---------------------------------------------------------------------------
# Subscription confirmation path
# ---------------------------------------------------------------------------

def test_subscription_confirmation_schedules_get(client, monkeypatch):
    """SubscriptionConfirmation messages must trigger a GET to SubscribeURL."""
    captured: dict[str, str] = {}

    async def _stub_confirm(url: str) -> None:
        captured["url"] = url

    # Replace the actual httpx call with a stub.
    monkeypatch.setattr(
        "app.api.routes.webhooks._confirm_sns_subscription",
        _stub_confirm,
    )

    confirmation = {
        "Type": "SubscriptionConfirmation",
        "MessageId": "abc",
        "Token": "xyz",
        "TopicArn": "arn:aws:sns:us-east-1:123:topic",
        "Message": "You have been invited to subscribe...",
        "SubscribeURL": "https://sns.us-east-1.amazonaws.com/?Action=ConfirmSubscription&Token=xyz",
        "Timestamp": "2026-05-07T12:00:00.000Z",
    }
    resp = client.post("/webhooks/cloudwatch-alarm", json=confirmation)
    assert resp.status_code == 200
    assert resp.json()["status"] == "confirming_subscription"

    # The background task should have called the stub with the SubscribeURL.
    assert captured.get("url", "").startswith("https://sns.us-east-1.amazonaws.com/")


def test_subscription_confirmation_without_url_fails(client):
    bad = {
        "Type": "SubscriptionConfirmation",
        "TopicArn": "arn:aws:sns:us-east-1:123:topic",
        # SubscribeURL deliberately missing
    }
    resp = client.post("/webhooks/cloudwatch-alarm", json=bad)
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

def test_token_required_when_configured(client, monkeypatch):
    """Configured token rejects requests without it."""
    from app.core import config as config_module
    monkeypatch.setattr(config_module.settings, "cloudwatch_webhook_token", "s3cr3t")

    resp = client.post("/webhooks/cloudwatch-alarm", json=_alarm_notification())
    assert resp.status_code == 401


def test_token_in_header_accepted(client, monkeypatch):
    from app.core import config as config_module
    monkeypatch.setattr(config_module.settings, "cloudwatch_webhook_token", "s3cr3t")

    resp = client.post(
        "/webhooks/cloudwatch-alarm",
        json=_alarm_notification(),
        headers={"X-Webhook-Token": "s3cr3t"},
    )
    assert resp.status_code == 200


def test_token_in_query_param_accepted(client, monkeypatch):
    """SNS HTTPS subscriptions can encode the token in the URL itself."""
    from app.core import config as config_module
    monkeypatch.setattr(config_module.settings, "cloudwatch_webhook_token", "s3cr3t")

    resp = client.post(
        "/webhooks/cloudwatch-alarm?token=s3cr3t",
        json=_alarm_notification(),
    )
    assert resp.status_code == 200


def test_wrong_token_rejected(client, monkeypatch):
    from app.core import config as config_module
    monkeypatch.setattr(config_module.settings, "cloudwatch_webhook_token", "s3cr3t")

    resp = client.post(
        "/webhooks/cloudwatch-alarm?token=wrong",
        json=_alarm_notification(),
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Bad input
# ---------------------------------------------------------------------------

def test_invalid_json_returns_400(client):
    resp = client.post(
        "/webhooks/cloudwatch-alarm",
        data="not-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 400


def test_unsupported_sns_type_returns_400(client):
    resp = client.post(
        "/webhooks/cloudwatch-alarm",
        json={"Type": "SomethingWeird", "Message": "x"},
    )
    assert resp.status_code == 400


def test_notification_with_freeform_message_still_ingests(client):
    """A Notification whose Message isn't valid JSON should still produce
    an event with sensible fallback fields."""
    payload = {
        "Type": "Notification",
        "Subject": "Custom alert: payment failure",
        "Message": "this is plain text not JSON",
        "TopicArn": "arn:aws:sns:us-east-1:123:topic",
    }
    resp = client.post("/webhooks/cloudwatch-alarm", json=payload)
    # Free-text path: no NewStateValue means we treat as ALARM and ingest.
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"
