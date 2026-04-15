"""
GitHub webhook — triggers Code Review Agent on PR open/update.
POST /webhooks/github
"""
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.core.config import settings
from app.services.alerting import Alert, Severity, alerting_service

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
