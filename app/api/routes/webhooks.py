"""
Webhooks:
  - POST /webhooks/github             — Code Review Agent on PR open/update,
                                        Monitor Generation Agent on PR merge.
  - POST /webhooks/cloudwatch-alarm   — push-based ingest from CloudWatch
                                        alarms via SNS. Drives system MTTD
                                        from "human attention lag" to
                                        single-digit minutes without polling
                                        production.
"""
import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.api.websocket_dashboard import broadcast
from app.core.config import settings
from app.models.events import ErrorEvent, EventSource
from app.services.alerting import Alert, Severity, alerting_service
from app.services.pending_events import pending_event_store

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


def _verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _run_code_review(pr_number: int, repo: str) -> None:
    logger.info("Code Review Agent: %s#%d", repo, pr_number)
    try:
        from app.agents.code_review import CodeReviewAgent
        agent = CodeReviewAgent()
        result = await agent.run(
            f"Review PR #{pr_number} in repo {repo}. Post the review as a GitHub comment."
        )
        logger.info("Review posted for %s#%d: %s", repo, pr_number, result.answer[:120])
    except Exception as exc:
        logger.error("Code review failed for %s#%d: %s", repo, pr_number, exc)


@router.post("/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.body()
    event = request.headers.get("X-GitHub-Event", "")
    signature = request.headers.get("X-Hub-Signature-256", "")

    secret: str = getattr(settings, "github_webhook_secret", "")
    if secret:
        if not signature:
            raise HTTPException(status_code=401, detail="Missing signature")
        if not _verify_signature(payload, signature, secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

    data = json.loads(payload)

    if event == "pull_request":
        action = data.get("action")
        pr = data.get("pull_request", {})
        pr_number: int = pr.get("number", 0)
        repo: str = data["repository"]["full_name"]

        if action in ("opened", "synchronize", "reopened"):
            background_tasks.add_task(_run_code_review, pr_number, repo)
            return {"status": "queued", "event": event, "pr": pr_number, "repo": repo}

        if action == "closed" and pr.get("merged"):
            background_tasks.add_task(_notify_pr_merged, pr, repo)
            background_tasks.add_task(_run_monitor_generation, pr, repo)
            return {"status": "queued", "event": "pr_merged", "pr": pr_number, "repo": repo}

    if event == "workflow_run":
        run = data.get("workflow_run", {})
        if data.get("action") == "completed" and run.get("conclusion") == "failure":
            background_tasks.add_task(_notify_build_failed, run, data["repository"]["full_name"])
            return {"status": "notified", "event": "build_failed"}

    return {"status": "ignored", "event": event}


async def _run_monitor_generation(pr: dict, repo: str) -> None:
    """Generate monitoring coverage for a merged PR (runs out-of-band)."""
    pr_number: int = pr.get("number", 0)
    pr_title: str = pr.get("title", "")
    pr_body: str = pr.get("body", "") or ""

    # Only run for the configured target repo — skip test/placeholder webhooks
    if settings.fix_target_repo and repo != settings.fix_target_repo:
        logger.info("[MonitorGen] Skipping %s#%d — not the target repo (%s)",
                    repo, pr_number, settings.fix_target_repo)
        return

    logger.info("[MonitorGen] Starting for %s#%d", repo, pr_number)
    try:
        owner, repo_name = repo.split("/", 1)
        from app.agents.monitor_generation import MonitorGenerationAgent
        from app.services.monitor_store import monitor_store
        agent = MonitorGenerationAgent()
        result = await agent.generate_monitors(
            owner=owner,
            repo=repo_name,
            pr_number=pr_number,
            pr_title=pr_title,
            pr_description=pr_body,
        )
        monitor_store.save(repo, pr_number, result)
        logger.info(
            "[MonitorGen] %s#%d — %d monitors generated (coverage: %.0f%%, dry_run=%s)",
            repo, pr_number, result.monitors_created,
            result.coverage_ratio * 100, result.dry_run,
        )
    except Exception as exc:
        logger.error("[MonitorGen] Failed for %s#%d: %s", repo, pr_number, exc)


async def _notify_pr_merged(pr: dict, repo: str) -> None:
    title = pr.get("title", "")
    number = pr.get("number", "")
    author = (pr.get("user") or {}).get("login", "unknown")
    merged_by = (pr.get("merged_by") or {}).get("login", "unknown")
    url = pr.get("html_url", "")
    base = (pr.get("base") or {}).get("ref", "")

    await alerting_service.send_alert(Alert(
        severity=Severity.INFO,
        title=f"PR merged: #{number} → {base}",
        message=(
            f"*<{url}|#{number}: {title}>*\n"
            f"Author: {author}  |  Merged by: {merged_by}  |  Repo: {repo}"
        ),
        source="GitHub",
        metadata={"repo": repo, "pr": number, "author": author, "merged_by": merged_by},
    ))


async def _notify_build_failed(run: dict, repo: str) -> None:
    workflow = run.get("name", "")
    branch = run.get("head_branch", "")
    run_number = run.get("run_number", "")
    actor = (run.get("actor") or {}).get("login", "unknown")
    url = run.get("html_url", "")
    commit_msg = (run.get("head_commit") or {}).get("message", "").split("\n")[0][:80]

    await alerting_service.send_alert(Alert(
        severity=Severity.ERROR,
        title=f"Build failed: {workflow} #{run_number}",
        message=(
            f"Workflow *{workflow}* failed on `{branch}`\n"
            f"Commit: _{commit_msg}_\n"
            f"Triggered by: {actor}  |  <{url}|View run>"
        ),
        source="GitHub",
        metadata={"repo": repo, "workflow": workflow, "branch": branch, "run_number": run_number},
    ))


# ---------------------------------------------------------------------------
# CloudWatch alarm webhook (SNS-shaped payload)
# ---------------------------------------------------------------------------

# AWS SNS publishes three message Types we care about:
#   - SubscriptionConfirmation: arrives once when the topic subscribes the
#     endpoint. Must be confirmed by GET-ing the SubscribeURL within ~3 days.
#   - Notification: every fired alarm. The "Message" field is itself a
#     JSON-encoded CloudWatch alarm payload.
#   - UnsubscribeConfirmation: arrives if someone unsubscribes; mostly
#     informational.
# Reference: https://docs.aws.amazon.com/sns/latest/dg/sns-message-and-json-formats.html


def _alarm_payload_to_event(alarm: Dict[str, Any]) -> ErrorEvent:
    """Normalise a CloudWatch alarm JSON payload into an ErrorEvent."""
    alarm_name = alarm.get("AlarmName", "unknown_alarm")
    new_state = alarm.get("NewStateValue", "")
    reason = alarm.get("NewStateReason", "")
    region = alarm.get("Region", "")
    arn = alarm.get("AlarmArn", "")
    trigger = alarm.get("Trigger", {}) or {}
    metric = trigger.get("MetricName", "")
    namespace = trigger.get("Namespace", "")

    # Extract service name from alarm dimensions. SNS payloads sometimes
    # use lowercased keys (`name`/`value`), sometimes Capitalised — handle both.
    service = "unknown"
    for dim in trigger.get("Dimensions") or []:
        key = (dim.get("name") or dim.get("Name") or "").lower()
        if key in ("servicename", "service", "function", "functionname"):
            service = dim.get("value") or dim.get("Value") or "unknown"
            break

    error_type = (
        f"CW_ALARM_{namespace.replace('/', '_').upper()}_{metric.upper()}"
        if namespace and metric
        else "CW_ALARM"
    )

    title = f"CloudWatch alarm: {alarm_name} ({new_state})"
    description = (
        f"Alarm: {alarm_name}\n"
        f"State: {new_state}\n"
        f"Reason: {reason}\n"
        f"Metric: {namespace}/{metric}\n"
        f"Region: {region}"
    )

    return ErrorEvent(
        source=EventSource.CLOUDWATCH,
        error_type=error_type,
        title=title,
        description=description,
        service=service,
        resource_id=arn,
        metadata={
            "alarm_name": alarm_name,
            "alarm_arn": arn,
            "new_state": new_state,
            "metric_name": metric,
            "namespace": namespace,
            "region": region,
            "log_group": "",  # not directly carried in alarm payloads
        },
    )


async def _confirm_sns_subscription(subscribe_url: str) -> None:
    """GET the SubscribeURL to complete an SNS HTTPS subscription."""
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(subscribe_url)
            if 200 <= resp.status_code < 300:
                logger.info("[SNS] Subscription confirmed")
            else:
                logger.warning(
                    "[SNS] Subscription confirmation returned HTTP %d", resp.status_code,
                )
    except Exception as exc:
        logger.error("[SNS] Failed to confirm subscription: %s", exc)


def _check_webhook_token(request: Request) -> None:
    """Reject the request unless the configured shared-secret token matches.

    Token can come from the `X-Webhook-Token` header or the `?token=…` query
    string (SNS HTTPS subscriptions can encode it in the subscription URL).
    Constant-time comparison prevents timing attacks.

    Long-term answer is full SNS signature verification per
    https://docs.aws.amazon.com/sns/latest/dg/sns-verify-signature-of-message.html
    """
    expected = settings.cloudwatch_webhook_token
    if not expected:
        return  # auth disabled
    provided = request.headers.get("X-Webhook-Token") or request.query_params.get("token", "")
    if not provided or not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=401, detail="Invalid webhook token")


@router.post("/cloudwatch-alarm")
async def cloudwatch_alarm_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Dict[str, Any]:
    """Push-based ingest from CloudWatch alarms via SNS.

    SNS POSTs every alarm transition here. We:
      1. Confirm the subscription handshake on first receipt.
      2. Normalise alarm notifications into ErrorEvents.
      3. Push through the same `pending_event_store` gate as the manual scan
         so the PR #84 noise filter classifies caught/uncaught and the human
         approval queue sees one entry per content-signature.
    """
    _check_webhook_token(request)

    raw = await request.body()
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    msg_type = msg.get("Type", "")

    if msg_type == "SubscriptionConfirmation":
        subscribe_url = msg.get("SubscribeURL", "")
        topic_arn = msg.get("TopicArn", "")
        if subscribe_url:
            background_tasks.add_task(_confirm_sns_subscription, subscribe_url)
            logger.info("[SNS] Confirming subscription to %s", topic_arn)
            return {"status": "confirming_subscription", "topic_arn": topic_arn}
        raise HTTPException(status_code=400, detail="Missing SubscribeURL")

    if msg_type == "UnsubscribeConfirmation":
        logger.warning("[SNS] Unsubscribe received for %s", msg.get("TopicArn", ""))
        return {"status": "unsubscribed", "topic_arn": msg.get("TopicArn", "")}

    if msg_type != "Notification":
        # Strict: refuse types we don't handle so misconfigured POSTs surface.
        raise HTTPException(status_code=400, detail=f"Unsupported SNS Type: {msg_type!r}")

    inner = msg.get("Message", "")
    try:
        alarm = json.loads(inner) if isinstance(inner, str) else inner
    except (json.JSONDecodeError, TypeError):
        # Free-text Message — fall back to using the SNS Subject + Message verbatim.
        alarm = {"AlarmName": msg.get("Subject", "sns_notification"), "NewStateReason": str(inner)}
    if not isinstance(alarm, dict):
        alarm = {"AlarmName": msg.get("Subject", "sns_notification"), "NewStateReason": str(inner)}

    # Only ingest ALARM transitions. OK and INSUFFICIENT_DATA are
    # state-recovery / data-gap signals; they shouldn't open new incidents.
    new_state = alarm.get("NewStateValue", "")
    if new_state and new_state != "ALARM":
        logger.info(
            "[SNS] Skipping non-ALARM transition (state=%s) for %s",
            new_state, alarm.get("AlarmName", ""),
        )
        return {"status": "ignored_non_alarm", "state": new_state}

    event = _alarm_payload_to_event(alarm)
    pe, is_new = pending_event_store.add(event)
    if pe is None:
        return {"status": "dismissed_signature"}

    ws_type = "pending_event_added" if is_new else "pending_event_updated"
    await broadcast({"type": ws_type, "event": pending_event_store.serialize(pe)})

    return {
        "status": "queued",
        "event_id": pe.id,
        "is_new": is_new,
        "occurrences": pe.occurrences,
        "handling": pe.handling,
    }
