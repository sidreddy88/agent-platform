"""
GitHub Actions monitoring — fetches latest workflow runs for a repo/branch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)
GITHUB_API = "https://api.github.com"


@dataclass
class WorkflowRun:
    id: int
    workflow_name: str
    run_number: int
    status: str        # queued | in_progress | completed
    conclusion: str | None  # success | failure | cancelled | skipped | None
    branch: str
    commit_sha: str
    commit_message: str
    actor: str
    url: str
    created_at: str
    updated_at: str

    @property
    def healthy(self) -> bool:
        return self.conclusion in ("success", "skipped") or self.status == "in_progress"

    @property
    def display_conclusion(self) -> str:
        if self.status == "in_progress":
            return "IN PROGRESS"
        if self.status == "queued":
            return "QUEUED"
        return (self.conclusion or "unknown").upper()


class GitHubActionsService:
    def __init__(self) -> None:
        self._token: str = getattr(settings, "github_token", "")
        self._repo: str = getattr(settings, "github_actions_repo", "")

    @property
    def _configured(self) -> bool:
        return bool(self._token and self._repo)

    def _headers(self) -> dict:
        return {
            "Authorization": f"token {self._token}",
            "Accept": "application/vnd.github.v3+json",
        }

    async def get_latest_runs(self, branch: str = "master", limit: int = 5) -> list[WorkflowRun]:
        if not self._configured:
            return []

        url = f"{GITHUB_API}/repos/{self._repo}/actions/runs"
        params = {"branch": branch, "per_page": limit}

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=self._headers(), params=params)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("GitHub Actions: failed to fetch runs for %s: %s", self._repo, exc)
            return []

        runs = []
        for r in data.get("workflow_runs", [])[:limit]:
            commit_msg = (r.get("head_commit") or {}).get("message", "")
            # Truncate to first line
            commit_msg = commit_msg.split("\n")[0][:80]
            runs.append(WorkflowRun(
                id=r["id"],
                workflow_name=r.get("name", ""),
                run_number=r.get("run_number", 0),
                status=r.get("status", ""),
                conclusion=r.get("conclusion"),
                branch=r.get("head_branch", branch),
                commit_sha=r.get("head_sha", "")[:7],
                commit_message=commit_msg,
                actor=(r.get("actor") or {}).get("login", ""),
                url=r.get("html_url", ""),
                created_at=r.get("created_at", ""),
                updated_at=r.get("updated_at", ""),
            ))
        return runs


github_actions_service = GitHubActionsService()
