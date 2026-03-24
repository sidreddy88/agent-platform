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

    if event == "pull_request" and data.get("action") in ("opened", "synchronize", "reopened"):
        pr_number: int = data["pull_request"]["number"]
        repo: str = data["repository"]["full_name"]
        background_tasks.add_task(_run_code_review, pr_number, repo)
        return {"status": "queued", "event": event, "pr": pr_number, "repo": repo}

    return {"status": "ignored", "event": event}
