"""
FixGenerationAgent — fetches the real file from GitHub, uses a single focused
LLM call to generate the fix, then creates a GitHub Issue and PR directly
via the GitHub API (no ReAct loop — deterministic sequential steps).

Flow:
  1. GitHub API  → fetch routes/services/image.js
  2. LLM call    → extract old function + generate fixed version
  3. GitHub API  → create Issue
  4. GitHub API  → create branch, commit fix, open PR

Output (FixResult):
  issue_url, pr_url, pr_number, branch, fix_description, files_changed, test_added
"""
from __future__ import annotations

import logging
import re
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


# ---------------------------------------------------------------------------
# FixGenerationAgent
# ---------------------------------------------------------------------------

class FixGenerationAgent(BaseAgent):
    """
    Generates and commits an AI fix for a diagnosed incident.

    Uses direct GitHub API calls + a single focused LLM call instead of a
    ReAct loop — the steps are deterministic so an agent loop adds no value
    and is unreliable (model can answer without calling tools).

    Usage:
        agent = FixGenerationAgent()
        result = await agent.fix(incident)
        print(result.pr_url)
    """

    def __init__(self, github: GitHubService | None = None) -> None:
        super().__init__(llm=LLMService())
        self._github = github or GitHubService()
        self._owner, self._repo = settings.fix_target_repo.split("/", 1)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fix(self, incident: IncidentState) -> FixResult:
        """Generate a fix and open a GitHub PR."""
        result, _ = await self.fix_with_steps(incident)
        return result

    async def fix_with_steps(self, incident: IncidentState) -> tuple[FixResult, list[str]]:
        """
        Same as fix() but also returns a list of step strings for debugging.
        Each string describes what happened at that step (✓ success / ✗ failure).
        """
        steps: list[str] = []
        event = incident.error_event
        today = datetime.utcnow().strftime("%Y-%m-%d")
        sev = str(event.severity).split(".")[-1] if event.severity else "P2"
        file_path = "routes/services/image.js"
        branch_name = (
            f"fix/{event.error_type or 'incident'}-{incident.id[:8]}".lower()
            .replace("_", "-")
        )

        def _fail(desc: str, issue_url: str | None = None) -> tuple[FixResult, list[str]]:
            return FixResult(
                issue_url=issue_url,
                pr_url=None,
                pr_number=None,
                branch=branch_name,
                fix_description=desc,
            ), steps

        # ── 1. Fetch the file from GitHub ──────────────────────────────
        try:
            content, file_sha = await self._github.get_file_contents(
                self._owner, self._repo, file_path
            )
            steps.append(f"✓ Fetched {file_path} (sha={file_sha[:8]}, {len(content)} chars)")
            logger.info("[FixGen] Fetched %s (%d chars)", file_path, len(content))
        except GitHubError as exc:
            steps.append(f"✗ get_file_contents failed: {exc}")
            logger.error("[FixGen] Failed to fetch file: %s", exc)
            return _fail(f"Could not fetch {file_path}: {exc}")

        # ── 2. Generate fix via LLM ────────────────────────────────────
        try:
            old_function, new_function = await self._generate_fix(content)
        except Exception as exc:
            steps.append(f"✗ LLM fix generation failed: {exc}")
            logger.error("[FixGen] LLM error: %s", exc)
            return _fail(f"LLM error: {exc}")

        if not old_function:
            steps.append("✗ LLM could not locate moveAndRemoveFileFromS3 in the file")
            return _fail("moveAndRemoveFileFromS3 not found in file")

        steps.append(
            f"✓ Generated fix (old={len(old_function)} chars, new={len(new_function)} chars)"
        )
        logger.info("[FixGen] Generated fix")

        # ── 3. Create GitHub Issue ─────────────────────────────────────
        issue_url: str | None = None
        issue_number: int | None = None
        issue_body = (
            f"## Summary\n"
            f"- **Error:** `NoSuchKey` in `moveAndRemoveFileFromS3`\n"
            f"- **Occurrences:** {incident.occurrences_24h} in last 24 hours\n"
            f"- **Root cause:** {incident.diagnosis}\n"
            f"- **Affected file:** `{file_path}`\n"
            f"- **Agent confidence:** {incident.confidence:.0%}\n"
            f"- **Detected:** {today}\n\n"
            f"## Fix approach\n"
            f"Wrap the S3 copy/delete in a try/catch — catch `NoSuchKey` specifically,\n"
            f"log a warning and return early. Re-throw anything else."
        )
        try:
            issue_number, issue_url = await self._github.create_issue(
                self._owner, self._repo,
                title=f"[{sev}] moveAndRemoveFileFromS3 throws NoSuchKey on missing S3 keys",
                body=issue_body,
                labels=["bug", "ai-detected", sev.lower()],
            )
            steps.append(f"✓ Created Issue #{issue_number}: {issue_url}")
            logger.info("[FixGen] Created issue #%d", issue_number)
        except GitHubError as exc:
            steps.append(f"✗ create_issue failed (continuing without issue link): {exc}")
            logger.warning("[FixGen] Issue creation failed: %s", exc)

        # ── 4. Apply fix, commit on branch, open PR ────────────────────
        # Replace the old function in the file content
        if old_function in content:
            new_content = content.replace(old_function, new_function, 1)
        elif old_function.strip() in content:
            new_content = content.replace(old_function.strip(), new_function.strip(), 1)
        else:
            steps.append("✗ old_function not found verbatim in file — cannot apply patch")
            logger.error("[FixGen] old_function not found in content")
            return _fail("old_function not found in file — LLM may have altered it", issue_url)

        pr_body = (
            f"## Summary\n"
            f"- Wraps S3 copy/delete in try/catch\n"
            f"- Catches `NoSuchKey` specifically, logs warning, returns early\n"
            f"- Re-throws all other errors so they surface normally\n\n"
            f"{f'Fixes #{issue_number}' if issue_number else ''}\n\n"
            f"**Incident ID:** {incident.id}  \n"
            f"**Agent confidence:** {incident.confidence:.0%}"
        )

        try:
            base_sha = await self._github.get_branch_sha(self._owner, self._repo, "main")
            await self._github.create_branch(self._owner, self._repo, branch_name, base_sha)
            steps.append(f"✓ Created branch {branch_name}")

            issue_ref = f"Fixes #{issue_number}" if issue_number else ""
            commit_sha = await self._github.update_file(
                self._owner, self._repo, file_path, new_content,
                f"fix: handle NoSuchKey gracefully in {file_path.split('/')[-1]}\n\n{issue_ref}",
                branch_name, file_sha,
            )
            steps.append(f"✓ Committed fix (sha={commit_sha[:8]})")
            logger.info("[FixGen] Committed fix on branch %s", branch_name)

            pr_number, pr_url = await self._github.create_pull_request(
                self._owner, self._repo,
                title="fix: handle NoSuchKey gracefully in moveAndRemoveFileFromS3",
                body=pr_body,
                head=branch_name,
                base="main",
                labels=["bug", "ai-generated-fix", "awaiting-review"],
            )
            steps.append(f"✓ Created PR #{pr_number}: {pr_url}")
            logger.info("[FixGen] Created PR #%d: %s", pr_number, pr_url)

        except GitHubError as exc:
            steps.append(f"✗ GitHub error during PR creation: {exc}")
            logger.error("[FixGen] GitHub error: %s", exc)
            return _fail(str(exc), issue_url)

        return FixResult(
            issue_url=issue_url,
            pr_url=pr_url,
            pr_number=pr_number,
            branch=branch_name,
            fix_description=f"NoSuchKey try/catch added to {file_path}",
            files_changed=[file_path],
            test_added=False,
            commit_sha=commit_sha,
        ), steps

    # ------------------------------------------------------------------
    # LLM fix generation
    # ------------------------------------------------------------------

    async def _generate_fix(self, content: str) -> tuple[str, str]:
        """
        Single focused LLM call: extract the current function + generate the fix.
        Returns (old_function_text, new_function_text).
        """
        prompt = f"""You are a JavaScript engineer fixing a production bug.

FILE: routes/services/image.js
```javascript
{content[:8000]}
```

TASK:
1. Find the complete `moveAndRemoveFileFromS3` function in the file above.
2. Generate a fixed version that wraps the S3 operations in try/catch,
   catching the `NoSuchKey` error code specifically and returning early.

Output ONLY the two blocks below — no explanation, no markdown, no other text:

OLD_FUNCTION:
<copy the function text EXACTLY as it appears in the file above, character for character>
END_OLD

NEW_FUNCTION:
<the fixed version — same signature and guard checks, add try/catch around S3 calls>
END_NEW

Fix pattern to apply in NEW_FUNCTION:
  async function moveAndRemoveFileFromS3(bucket, imageObj) {{
    try {{
      // keep all existing guard checks here
      await s3.copyObject({{ ... }}).promise();
      await s3.deleteObject({{ Bucket: bucket, Key: imageObj.source }}).promise();
    }} catch (error) {{
      if (error.code === 'NoSuchKey') {{
        console.warn('moveAndRemoveFileFromS3: source key not found, skipping',
          {{ bucket, source: imageObj.source }});
        return;
      }}
      console.log('moveAndRemoveFileFromS3 error', error, bucket, imageObj);
    }}
  }}"""

        response = await self._llm.complete(
            messages=[{"role": "user", "content": prompt}],
            system=(
                "You are a JavaScript engineer. "
                "Output ONLY the OLD_FUNCTION and NEW_FUNCTION blocks as specified. "
                "Do not include any explanation or markdown fences."
            ),
        )

        old_match = re.search(r"OLD_FUNCTION:\n(.*?)END_OLD", response, re.DOTALL)
        new_match = re.search(r"NEW_FUNCTION:\n(.*?)END_NEW", response, re.DOTALL)

        if not old_match or not new_match:
            logger.error("[FixGen] LLM response missing expected blocks: %s", response[:400])
            return "", ""

        return old_match.group(1), new_match.group(1)
