import base64
import gzip
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings

GITHUB_API = "https://api.github.com"


@dataclass
class PRDetails:
    number: int
    title: str
    description: str | None
    author: str
    head_branch: str
    base_branch: str
    head_sha: str


@dataclass
class FileDiff:
    filename: str
    status: str  # added, removed, modified, renamed, etc.
    additions: int
    deletions: int
    patch: str | None  # may be absent for binary files


class GitHubError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API error {status}: {message}")
        self.status = status


class GitHubService:
    def __init__(self, token: str | None = None) -> None:
        tok = token or settings.github_token
        if not tok:
            raise ValueError("GitHub token is required (set GITHUB_TOKEN in .env)")
        self._headers = {
            "Authorization": f"Bearer {tok}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=GITHUB_API,
            headers=self._headers,
            timeout=30.0,
        )

    @staticmethod
    async def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code == 429:
            reset = response.headers.get("X-RateLimit-Reset", "unknown")
            raise GitHubError(429, f"Rate limit exceeded. Resets at epoch {reset}.")
        if response.status_code >= 400:
            try:
                msg = response.json().get("message", response.text)
            except Exception:
                msg = response.text
            raise GitHubError(response.status_code, msg)

    async def get_pr(self, owner: str, repo: str, pr_number: int) -> PRDetails:
        """Fetch title, description, author, and branch info for a PR."""
        async with self._client() as client:
            response = await client.get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
            await self._raise_for_status(response)
            data = response.json()

        return PRDetails(
            number=data["number"],
            title=data["title"],
            description=data.get("body"),
            author=data["user"]["login"],
            head_branch=data["head"]["ref"],
            base_branch=data["base"]["ref"],
            head_sha=data["head"]["sha"],
        )

    async def get_pr_diff(
        self, owner: str, repo: str, pr_number: int
    ) -> list[FileDiff]:
        """Fetch the list of changed files with diffs for a PR."""
        files: list[FileDiff] = []
        page = 1

        async with self._client() as client:
            while True:
                response = await client.get(
                    f"/repos/{owner}/{repo}/pulls/{pr_number}/files",
                    params={"per_page": 100, "page": page},
                )
                await self._raise_for_status(response)
                batch = response.json()
                if not batch:
                    break

                for f in batch:
                    files.append(
                        FileDiff(
                            filename=f["filename"],
                            status=f["status"],
                            additions=f["additions"],
                            deletions=f["deletions"],
                            patch=f.get("patch"),  # absent for binary / very large files
                        )
                    )

                if len(batch) < 100:
                    break
                page += 1

        return files

    async def post_review_comment(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        commit_id: str,
        path: str,
        line: int,
        side: str = "RIGHT",
    ) -> dict:
        """Post an inline review comment on a specific line of a PR diff.

        Args:
            owner: Repository owner.
            repo: Repository name.
            pr_number: Pull request number.
            body: Comment text.
            commit_id: The SHA of the commit to comment on (use PRDetails.head_sha).
            path: Relative file path within the repo.
            line: Line number in the diff to attach the comment to.
            side: "RIGHT" (new file) or "LEFT" (old file). Defaults to "RIGHT".
        """
        payload = {
            "body": body,
            "commit_id": commit_id,
            "path": path,
            "line": line,
            "side": side,
        }
        async with self._client() as client:
            response = await client.post(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/comments",
                json=payload,
            )
            await self._raise_for_status(response)
            return response.json()

    # ------------------------------------------------------------------
    # CI/CD — workflow runs & logs
    # ------------------------------------------------------------------

    async def get_workflow_runs(
        self,
        owner: str,
        repo: str,
        limit: int = 10,
        status: str | None = None,
        branch: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return recent workflow runs for a repo.

        Args:
            limit:  Max runs to return (capped at 100).
            status: Optional filter — "failure", "success", "in_progress", etc.
            branch: Optional branch name filter.
        """
        params: dict[str, Any] = {"per_page": min(limit, 100)}
        if status:
            params["status"] = status
        if branch:
            params["branch"] = branch

        async with self._client() as client:
            response = await client.get(
                f"/repos/{owner}/{repo}/actions/runs",
                params=params,
            )
            await self._raise_for_status(response)
            data = response.json()

        runs = []
        for r in data.get("workflow_runs", [])[:limit]:
            runs.append({
                "id": r["id"],
                "name": r["name"],
                "status": r["status"],
                "conclusion": r.get("conclusion"),
                "branch": r["head_branch"],
                "commit_sha": r["head_sha"][:8],
                "commit_message": r["head_commit"]["message"].splitlines()[0] if r.get("head_commit") else "",
                "created_at": r["created_at"],
                "html_url": r["html_url"],
            })
        return runs

    async def get_run_jobs(
        self, owner: str, repo: str, run_id: int
    ) -> list[dict[str, Any]]:
        """Return jobs (and their steps) for a workflow run."""
        async with self._client() as client:
            response = await client.get(
                f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"
            )
            await self._raise_for_status(response)
            data = response.json()

        jobs = []
        for j in data.get("jobs", []):
            jobs.append({
                "id": j["id"],
                "name": j["name"],
                "status": j["status"],
                "conclusion": j.get("conclusion"),
                "steps": [
                    {
                        "name": s["name"],
                        "conclusion": s.get("conclusion"),
                        "number": s["number"],
                    }
                    for s in j.get("steps", [])
                    if s.get("conclusion") in ("failure", "timed_out", None)
                    or s["status"] != "completed"
                ],
            })
        return jobs

    async def get_job_logs(self, owner: str, repo: str, job_id: int) -> str:
        """Download and return plain-text logs for a specific job.

        GitHub returns a redirect to a pre-signed URL; httpx follows it
        automatically. Logs may be gzip-compressed.
        """
        async with self._client() as client:
            response = await client.get(
                f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                follow_redirects=True,
            )
            # Logs endpoint returns 302 → raw log content (200)
            if response.status_code >= 400:
                await self._raise_for_status(response)

        content = response.content
        # Some responses are gzip-compressed despite no Content-Encoding header
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)

        return content.decode("utf-8", errors="replace")

    async def get_run_logs(self, owner: str, repo: str, run_id: int) -> str:
        """Fetch logs for all *failed* jobs in a run and return them combined."""
        jobs = await self.get_run_jobs(owner, repo, run_id)
        failed_jobs = [j for j in jobs if j["conclusion"] in ("failure", "timed_out")]

        if not failed_jobs:
            # Fall back to all jobs if nothing is explicitly failed
            failed_jobs = jobs

        parts: list[str] = []
        async with self._client():
            for job in failed_jobs:
                parts.append(f"\n=== Job: {job['name']} ({job['conclusion']}) ===\n")
                try:
                    logs = await self.get_job_logs(owner, repo, job["id"])
                    # Keep last 300 lines — most relevant errors are at the bottom
                    tail = "\n".join(logs.splitlines()[-300:])
                    parts.append(tail)
                except GitHubError as exc:
                    parts.append(f"(Could not fetch logs: {exc})")

        return "\n".join(parts) if parts else "No logs available."

    # ------------------------------------------------------------------
    # Fix generation — file read/write, issues, PRs
    # ------------------------------------------------------------------

    async def get_file_contents(
        self, owner: str, repo: str, path: str, ref: str = "main"
    ) -> tuple[str, str]:
        """Return (decoded_content, sha) for a file at a given ref."""
        async with self._client() as client:
            response = await client.get(
                f"/repos/{owner}/{repo}/contents/{path}",
                params={"ref": ref},
            )
            await self._raise_for_status(response)
            data = response.json()
        content = base64.b64decode(data["content"]).decode("utf-8")
        return content, data["sha"]

    async def get_default_branch(self, owner: str, repo: str) -> str:
        """Return the default branch name (e.g. 'main' or 'master')."""
        async with self._client() as client:
            response = await client.get(f"/repos/{owner}/{repo}")
            await self._raise_for_status(response)
            return response.json()["default_branch"]

    async def get_file_tree(self, owner: str, repo: str, ref: str = "main") -> tuple[set[str], str]:
        """Return (set_of_file_paths, tree_sha) for the repo at ref.

        Uses the recursive Git Trees endpoint — one API call for the full tree.
        The sha is returned so callers can cache by (repo, sha).
        """
        async with self._client() as client:
            resp = await client.get(
                f"/repos/{owner}/{repo}/git/trees/{ref}",
                params={"recursive": "1"},
            )
            await self._raise_for_status(resp)
            data = resp.json()
        paths = {item["path"] for item in data.get("tree", []) if item.get("type") == "blob"}
        return paths, data["sha"]

    async def search_code(self, owner: str, repo: str, query: str) -> list[dict]:
        """
        Search file *contents* in the repo using GitHub Code Search API.

        Returns a list of dicts with 'path' and 'fragment' (matched code snippet).
        Skips node_modules and test files. Max 5 results.
        """
        _SKIP = ("node_modules", ".test.", ".spec.", "dist/", "build/")
        try:
            async with self._client() as client:
                resp = await client.get(
                    "/search/code",
                    params={"q": f"{query} repo:{owner}/{repo}", "per_page": 10},
                    headers={"Accept": "application/vnd.github.v3.text-match+json"},
                )
                if resp.status_code != 200:
                    return []
                items = resp.json().get("items", [])
                results = []
                for item in items:
                    path = item.get("path", "")
                    if any(s in path for s in _SKIP):
                        continue
                    fragment = ""
                    matches = item.get("text_matches", [])
                    if matches:
                        fragment = matches[0].get("fragment", "")[:200]
                    results.append({"path": path, "fragment": fragment})
                    if len(results) >= 5:
                        break
                return results
        except Exception:
            return []

    async def search_files_by_keyword(self, owner: str, repo: str, keyword: str, ref: str = "main") -> list[str]:
        """Return all blob paths in the repo whose path contains keyword (case-insensitive). Max 50 results."""
        try:
            async with self._client() as client:
                ref_resp = await client.get(f"/repos/{owner}/{repo}/git/ref/heads/{ref}")
                if ref_resp.status_code != 200:
                    return []
                tree_sha = ref_resp.json()["object"]["sha"]
                tree_resp = await client.get(
                    f"/repos/{owner}/{repo}/git/trees/{tree_sha}",
                    params={"recursive": "1"},
                )
                if tree_resp.status_code != 200:
                    return []
                kw = keyword.lower()
                return [
                    item["path"] for item in tree_resp.json().get("tree", [])
                    if item.get("type") == "blob" and (not kw or kw in item["path"].lower())
                ][:50]
        except Exception:
            return []

    async def find_files_by_name(self, owner: str, repo: str, filename: str, ref: str = "main") -> list[str]:
        """Return all repo paths whose basename matches filename (recursive tree search)."""
        try:
            async with self._client() as client:
                # Get the tree SHA for the ref
                ref_resp = await client.get(f"/repos/{owner}/{repo}/git/ref/heads/{ref}")
                if ref_resp.status_code != 200:
                    return []
                tree_sha = ref_resp.json()["object"]["sha"]
                tree_resp = await client.get(
                    f"/repos/{owner}/{repo}/git/trees/{tree_sha}",
                    params={"recursive": "1"},
                )
                if tree_resp.status_code != 200:
                    return []
                items = tree_resp.json().get("tree", [])
            return [
                item["path"] for item in items
                if item.get("type") == "blob" and item["path"].rsplit("/", 1)[-1] == filename
            ]
        except Exception:
            return []

    async def get_branch_sha(
        self, owner: str, repo: str, branch: str = "main"
    ) -> str:
        """Return the HEAD commit SHA of a branch."""
        async with self._client() as client:
            response = await client.get(
                f"/repos/{owner}/{repo}/git/ref/heads/{branch}"
            )
            await self._raise_for_status(response)
            return response.json()["object"]["sha"]

    async def delete_branch(self, owner: str, repo: str, branch_name: str) -> None:
        """Delete a branch (best-effort — ignores 404)."""
        async with self._client() as client:
            response = await client.delete(
                f"/repos/{owner}/{repo}/git/refs/heads/{branch_name}"
            )
            if response.status_code not in (204, 404, 422):
                await self._raise_for_status(response)

    async def create_branch(
        self, owner: str, repo: str, branch_name: str, from_sha: str
    ) -> None:
        """Create a new branch from a commit SHA, deleting any stale branch of the same name first."""
        async with self._client() as client:
            response = await client.post(
                f"/repos/{owner}/{repo}/git/refs",
                json={"ref": f"refs/heads/{branch_name}", "sha": from_sha},
            )
            if response.status_code == 422:
                # Branch already exists from a prior attempt — delete and retry
                await self.delete_branch(owner, repo, branch_name)
                response = await client.post(
                    f"/repos/{owner}/{repo}/git/refs",
                    json={"ref": f"refs/heads/{branch_name}", "sha": from_sha},
                )
            await self._raise_for_status(response)

    async def update_file(
        self,
        owner: str,
        repo: str,
        path: str,
        content: str,
        message: str,
        branch: str,
        sha: str | None = None,
    ) -> str:
        """Commit a file create or update on a branch. Returns the new commit SHA.

        Pass sha=None to create a new file; pass the existing sha to update.
        On 409 (stale SHA), re-fetches the current SHA for the branch and retries once.
        """
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")

        async def _attempt(current_sha: str | None) -> httpx.Response:
            payload: dict = {"message": message, "content": encoded, "branch": branch}
            if current_sha:
                payload["sha"] = current_sha
            async with self._client() as client:
                return await client.put(
                    f"/repos/{owner}/{repo}/contents/{path}",
                    json=payload,
                )

        response = await _attempt(sha)
        if response.status_code == 409 and sha is not None:
            # SHA is stale — re-fetch from the branch and retry once
            _, fresh_sha = await self.get_file_contents(owner, repo, path, ref=branch)
            response = await _attempt(fresh_sha)

        await self._raise_for_status(response)
        return response.json()["commit"]["sha"]

    async def create_issue(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> tuple[int, str]:
        """Create a GitHub issue. Returns (issue_number, html_url)."""
        payload: dict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        async with self._client() as client:
            response = await client.post(f"/repos/{owner}/{repo}/issues", json=payload)
            await self._raise_for_status(response)
            data = response.json()
        return data["number"], data["html_url"]

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        labels: list[str] | None = None,
    ) -> tuple[int, str]:
        """Create a pull request. Returns (pr_number, html_url)."""
        async with self._client() as client:
            response = await client.post(
                f"/repos/{owner}/{repo}/pulls",
                json={"title": title, "body": body, "head": head, "base": base},
            )
            await self._raise_for_status(response)
            data = response.json()
        pr_number, pr_url = data["number"], data["html_url"]

        if labels:
            try:
                async with self._client() as client:
                    await client.post(
                        f"/repos/{owner}/{repo}/issues/{pr_number}/labels",
                        json={"labels": labels},
                    )
            except Exception:
                pass   # labels are non-critical

        return pr_number, pr_url

    async def is_pr_merged(self, owner: str, repo: str, pr_number: int) -> bool:
        """Return True if the pull request has been merged."""
        async with self._client() as client:
            response = await client.get(f"/repos/{owner}/{repo}/pulls/{pr_number}")
            await self._raise_for_status(response)
            return response.json().get("merged_at") is not None

    async def close_pull_request(self, owner: str, repo: str, pr_number: int) -> None:
        """Close a pull request without merging."""
        async with self._client() as client:
            response = await client.patch(
                f"/repos/{owner}/{repo}/pulls/{pr_number}",
                json={"state": "closed"},
            )
            await self._raise_for_status(response)

    async def get_commit_checks(self, owner: str, repo: str, ref: str) -> list[dict]:
        """Return CI check runs for a commit SHA or ref."""
        try:
            async with self._client() as client:
                response = await client.get(
                    f"/repos/{owner}/{repo}/commits/{ref}/check-runs",
                    params={"per_page": 100},
                    headers={"Accept": "application/vnd.github+json"},
                )
                await self._raise_for_status(response)
                data = response.json()
        except Exception:
            return []
        return [
            {
                "name": r["name"],
                "status": r["status"],
                "conclusion": r.get("conclusion"),
                "url": r.get("html_url"),
            }
            for r in data.get("check_runs", [])
        ]

    async def post_pr_review(
        self,
        owner: str,
        repo: str,
        pr_number: int,
        body: str,
        event: str = "COMMENT",
    ) -> dict:
        """Post a top-level PR review (not an inline comment).

        Args:
            event: One of "APPROVE", "REQUEST_CHANGES", or "COMMENT".
        """
        async with self._client() as client:
            response = await client.post(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/reviews",
                json={"body": body, "event": event},
            )
            await self._raise_for_status(response)
            return response.json()
