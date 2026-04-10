"""
FixGenerationAgent — reads the real function from GitHub, generates a scoped fix + test,
creates a GitHub Issue, commits the fix on a branch, and opens a PR.

Model: Claude Sonnet

Scope limits (enforced in prompt):
  ✅ Add try/catch or specific error handling
  ✅ Add existence / guard checks
  ✅ Add logging
  ❌ Refactor logic
  ❌ Change function signatures
  ❌ Rewrite complex business logic

Flow:
  1. get_file_contents  → read the affected file from GitHub
  2. create_github_issue → open a tracked issue with full context
  3. create_pr_with_fix  → branch → commit fix (+ optional test) → open PR
  4. Answer: summary with issue URL + PR URL

Output (FixResult):
  issue_url, pr_url, pr_number, branch, fix_description, files_changed, test_added
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.agents.base import BaseAgent
from app.core.config import settings
from app.models.events import IncidentState
from app.services.github import GitHubError, GitHubService
from app.services.llm import LLMService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class FixResult:
    issue_url: str | None
    pr_url: str | None
    pr_number: int | None
    branch: str
    fix_description: str
    files_changed: list[str] = field(default_factory=list)
    test_added: bool = False
    commit_sha: str | None = None


def _parse_fix_result(answer: str, branch: str) -> FixResult:
    """Extract issue URL and PR URL from the agent's final Answer."""
    pr_url_match = re.search(r"https://github\.com/[^\s)\"]+/pull/\d+", answer)
    issue_url_match = re.search(r"https://github\.com/[^\s)\"]+/issues/\d+", answer)

    pr_url = pr_url_match.group() if pr_url_match else None

    # Extract PR number from URL — more reliable than parsing the LLM's prose
    pr_number: int | None = None
    if pr_url:
        num_match = re.search(r"/pull/(\d+)", pr_url)
        if num_match:
            pr_number = int(num_match.group(1))

    return FixResult(
        issue_url=issue_url_match.group() if issue_url_match else None,
        pr_url=pr_url,
        pr_number=pr_number,
        branch=branch,
        fix_description=answer[:500],
        test_added="test" in answer.lower(),
    )


# ---------------------------------------------------------------------------
# FixGenerationAgent
# ---------------------------------------------------------------------------

class FixGenerationAgent(BaseAgent):
    """
    Generates and commits an AI fix for a diagnosed incident.

    Usage:
        agent = FixGenerationAgent()
        result = await agent.fix(incident)
        print(result.pr_url)
    """

    def __init__(self, github: GitHubService | None = None) -> None:
        super().__init__(llm=LLMService())   # Sonnet — code generation
        self._github = github or GitHubService()
        self._owner, self._repo = settings.fix_target_repo.split("/", 1)
        self._register_tools()

    def _register_tools(self) -> None:
        gh = self._github
        owner, repo = self._owner, self._repo

        async def _get_file_contents(path: str) -> str:
            """Fetch a file from the target repo. Returns the raw content."""
            try:
                content, sha = await gh.get_file_contents(owner, repo, path)
                # Cache SHA for commit step
                self._file_shas[path] = sha
                return f"File: {path} (sha={sha[:8]})\n\n{content}"
            except GitHubError as exc:
                return f"GitHub error fetching '{path}': {exc}"

        async def _create_github_issue(
            title: str,
            body: str,
            labels: list[str] | None = None,
        ) -> str:
            """Create a GitHub issue documenting the incident. Returns issue URL."""
            try:
                number, url = await gh.create_issue(
                    owner, repo, title, body,
                    labels=labels or ["bug", "ai-detected"],
                )
                self._issue_number = number
                return f"Issue #{number} created: {url}"
            except GitHubError as exc:
                return f"GitHub error creating issue: {exc}"

        async def _create_pr_with_fix(
            file_path: str,
            old_function: str,
            new_function: str,
            branch_name: str,
            pr_title: str,
            pr_body: str,
            test_file_path: str = "",
            test_content: str = "",
        ) -> str:
            """
            Apply a function replacement, commit to a new branch, and open a PR.

            old_function must match the current file content exactly.
            If test_file_path and test_content are provided, also commit the test.
            """
            try:
                # Re-fetch to get the current SHA (may differ from cached if file changed)
                content, file_sha = await gh.get_file_contents(owner, repo, file_path)

                if old_function not in content:
                    # Try stripping trailing whitespace differences
                    stripped_old = old_function.strip()
                    if stripped_old not in content:
                        return (
                            f"ERROR: old_function not found verbatim in {file_path}. "
                            "Fetch the file again with get_file_contents and copy the "
                            "function exactly as it appears."
                        )
                    new_content = content.replace(stripped_old, new_function.strip(), 1)
                else:
                    new_content = content.replace(old_function, new_function, 1)

                # Create branch from main
                base_sha = await gh.get_branch_sha(owner, repo, "main")
                await gh.create_branch(owner, repo, branch_name, base_sha)

                # Commit fix
                issue_ref = f"Fixes #{self._issue_number}" if self._issue_number else ""
                commit_sha = await gh.update_file(
                    owner, repo, file_path, new_content,
                    f"fix: handle NoSuchKey gracefully in {file_path.split('/')[-1]}\n\n{issue_ref}",
                    branch_name, file_sha,
                )
                files_changed = [file_path]
                self._files_changed = files_changed

                # Commit test if provided
                if test_file_path and test_content:
                    try:
                        _, test_sha = await gh.get_file_contents(owner, repo, test_file_path)
                    except GitHubError:
                        test_sha = ""   # new file

                    if test_sha:
                        await gh.update_file(
                            owner, repo, test_file_path, test_content,
                            f"test: add NoSuchKey handling test for {file_path.split('/')[-1]}",
                            branch_name, test_sha,
                        )
                    else:
                        # Create new test file
                        import base64
                        encoded = base64.b64encode(test_content.encode()).decode("ascii")
                        async with gh._client() as client:
                            await client.put(
                                f"/repos/{owner}/{repo}/contents/{test_file_path}",
                                json={
                                    "message": f"test: add NoSuchKey handling test",
                                    "content": encoded,
                                    "branch": branch_name,
                                },
                            )
                    files_changed.append(test_file_path)
                    self._files_changed = files_changed

                # Create PR
                pr_number, pr_url = await gh.create_pull_request(
                    owner, repo, pr_title, pr_body, branch_name, "main",
                    labels=["bug", "ai-generated-fix", "awaiting-review"],
                )
                self._pr_number = pr_number
                self._pr_url = pr_url

                return (
                    f"PR #{pr_number} created: {pr_url}\n"
                    f"Branch: {branch_name}\n"
                    f"Commit: {commit_sha[:8]}\n"
                    f"Files: {', '.join(files_changed)}"
                )

            except GitHubError as exc:
                return f"GitHub error: {exc}"
            except Exception as exc:
                return f"Unexpected error: {exc}"

        self.register_tool(
            "get_file_contents",
            _get_file_contents,
            (
                "Read a file from the target GitHub repo. "
                "Use this to see the exact current function before generating a fix. "
                "Input: {path: string (e.g. 'routes/services/image.js')}"
            ),
        )
        self.register_tool(
            "create_github_issue",
            _create_github_issue,
            (
                "Create a GitHub issue documenting the incident. Call before create_pr_with_fix. "
                "Input: {title: string, body: string, labels: [string] (optional)}"
            ),
        )
        self.register_tool(
            "create_pr_with_fix",
            _create_pr_with_fix,
            (
                "Apply a function-level fix, commit it on a branch, and open a PR. "
                "old_function must match the file content exactly (copy from get_file_contents). "
                "Optionally include a test file. "
                "Input: {file_path: string, old_function: string, new_function: string, "
                "branch_name: string, pr_title: string, pr_body: string, "
                "test_file_path: string (optional), test_content: string (optional)}"
            ),
        )

    async def fix(self, incident: IncidentState) -> FixResult:
        """Generate a fix for a diagnosed incident and open a GitHub PR."""
        # Reset per-run state
        self._file_shas: dict[str, str] = {}
        self._issue_number: int | None = None
        self._pr_number: int | None = None
        self._pr_url: str | None = None
        self._files_changed: list[str] = []

        event = incident.error_event
        branch_name = (
            f"fix/{event.error_type or 'incident'}-{incident.id[:8]}".lower()
            .replace("_", "-")
        )
        today = datetime.utcnow().strftime("%Y-%m-%d")

        prompt = f"""You are a senior engineer implementing an AI-generated fix for a production incident.

INCIDENT:
  error_type      : {event.error_type}
  service         : {event.service}
  severity        : {event.severity}
  occurrences_24h : {incident.occurrences_24h}
  task_id         : {event.task_id}

DIAGNOSIS (confidence {incident.confidence:.0%}):
  root_cause        : {incident.diagnosis}
  affected_function : moveAndRemoveFileFromS3
  affected_file     : routes/services/image.js (verify with get_file_contents)

STEPS — follow exactly:

1. Call get_file_contents with path="routes/services/image.js"
   Read the exact current moveAndRemoveFileFromS3 function.

2. Call create_github_issue with:
   title: "[{event.severity}] moveAndRemoveFileFromS3 throws NoSuchKey on missing S3 keys"
   body (markdown):
     ## Summary
     - **Error:** `NoSuchKey` in `moveAndRemoveFileFromS3`
     - **Occurrences:** {incident.occurrences_24h} in last 24 hours
     - **Root cause:** {incident.diagnosis}
     - **Affected file:** `routes/services/image.js`
     - **Agent confidence:** {incident.confidence:.0%}
     - **Detected:** {today}

     ## Evidence
     {chr(10).join(f'- {e}' for e in (incident.error_event.metadata.get('evidence', [])))}

     ## Fix approach
     Wrap the S3 copy/delete in a try/catch — catch `NoSuchKey` specifically,
     log a warning and return early. Re-throw anything else.
   labels: ["bug", "ai-detected", "{str(event.severity).split('.')[-1].lower() if event.severity else 'p2'}"]

3. Call create_pr_with_fix with:
   file_path: "routes/services/image.js"
   old_function: <exact current function from step 1>
   new_function: <fixed version — catch NoSuchKey, log warn, return early>
   branch_name: "{branch_name}"
   pr_title: "fix: handle NoSuchKey gracefully in moveAndRemoveFileFromS3"
   pr_body (markdown):
     ## Summary
     - Wraps S3 copy/delete in try/catch
     - Catches `NoSuchKey` specifically, logs warning and returns early
     - Re-throws all other errors so they surface normally
     - Adds Jest unit test for missing key scenario

     Fixes #{{issue_number}}

     ## Change scope
     - 1 file, ~10 lines
     - **Agent confidence:** {incident.confidence:.0%}
     - **Incident ID:** {incident.id}
   test_file_path: "routes/services/__tests__/image.test.js"
   test_content: <Jest test for NoSuchKey handling>

SCOPE LIMITS — ONLY apply these types of changes:
  ✅ Add try/catch or specific error handling around the S3 operations
  ✅ Add a guard check before the operation
  ✅ Add a warning log on graceful skip
  ❌ Refactor logic or rename variables
  ❌ Change the function signature
  ❌ Touch any other function

FIX PATTERN — prefer option B (specific catch, not existence check):
  // After: catch NoSuchKey specifically
  async function moveAndRemoveFileFromS3(bucket, imageObj) {{
    try {{
      if (!imageObj.source || !imageObj.destination) return;
      if (imageObj.source === imageObj.destination) return;
      await s3.copyObject({{ ... }}).promise();
      await s3.deleteObject({{ Bucket: bucket, Key: imageObj.source }}).promise();
    }} catch (error) {{
      if (error.code === 'NoSuchKey') {{
        console.warn('moveAndRemoveFileFromS3: source key not found, skipping', {{ bucket, source: imageObj.source }});
        return;
      }}
      console.log('moveAndRemoveFileFromS3 error', error, bucket, imageObj);
    }}
  }}

Answer with a concise summary. In your answer, include the EXACT issue URL and PR URL
returned by the tools — do not construct or guess URLs. Copy them verbatim from the
tool responses (they look like https://github.com/VoyageGroupMag/AllInterviews/issues/N
and https://github.com/VoyageGroupMag/AllInterviews/pull/N)."""

        result = await self.run(prompt)
        fix_result = _parse_fix_result(result.answer, branch_name)

        # Patch in cached tool-call values — always prefer these over regex-parsed text
        if self._pr_url and not fix_result.pr_url:
            fix_result.pr_url = self._pr_url
        if self._pr_number and not fix_result.pr_number:
            fix_result.pr_number = self._pr_number
        # If pr_url is now set but pr_number is still missing, extract from URL
        if fix_result.pr_url and not fix_result.pr_number:
            m = re.search(r"/pull/(\d+)", fix_result.pr_url)
            if m:
                fix_result.pr_number = int(m.group(1))
        if self._files_changed and not fix_result.files_changed:
            fix_result.files_changed = self._files_changed

        return fix_result
