from dataclasses import dataclass

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
