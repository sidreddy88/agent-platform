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

        # ── 1. Resolve default branch + fetch the file ─────────────────
        try:
            default_branch = await self._github.get_default_branch(self._owner, self._repo)
            steps.append(f"✓ Default branch: {default_branch}")
            logger.info("[FixGen] Default branch: %s", default_branch)
        except GitHubError as exc:
            default_branch = "main"
            steps.append(f"⚠ Could not detect default branch ({exc}) — assuming '{default_branch}'")

        try:
            content, file_sha = await self._github.get_file_contents(
                self._owner, self._repo, file_path, ref=default_branch
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
            base_sha = await self._github.get_branch_sha(
                self._owner, self._repo, default_branch
            )
            await self._github.create_branch(self._owner, self._repo, branch_name, base_sha)
            steps.append(f"✓ Created branch {branch_name} from {default_branch}")

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
                base=default_branch,
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
    # Fix generation
    # ------------------------------------------------------------------

    def _extract_js_function(self, content: str, function_name: str) -> str:
        """
        Extract a complete JavaScript function from source using brace counting.
        Handles async/regular functions and arrow functions assigned to const.
        Returns the exact text as it appears in the file, or "" if not found.
        """
        patterns = [
            rf'async\s+function\s+{re.escape(function_name)}\s*\(',
            rf'function\s+{re.escape(function_name)}\s*\(',
            rf'const\s+{re.escape(function_name)}\s*=\s*async\s*(?:function\s*)?\(',
            rf'const\s+{re.escape(function_name)}\s*=\s*function\s*\(',
        ]
        start_pos = -1
        for pattern in patterns:
            m = re.search(pattern, content)
            if m:
                start_pos = m.start()
                break

        if start_pos == -1:
            return ""

        brace_start = content.find("{", start_pos)
        if brace_start == -1:
            return ""

        depth = 0
        in_string = False
        string_char = ""
        escape_next = False

        for i in range(brace_start, len(content)):
            c = content[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\" and in_string:
                escape_next = True
                continue
            if in_string:
                if c == string_char:
                    in_string = False
                continue
            if c in ('"', "'", "`"):
                in_string = True
                string_char = c
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return content[start_pos : i + 1]

        return ""

    async def _generate_fix(self, content: str) -> tuple[str, str]:
        """
        Extract the function using brace-counting (exact text from file),
        then use a single LLM call to generate the fixed version.
        Returns (old_function_text, new_function_text).
        """
        old_function = self._extract_js_function(content, "moveAndRemoveFileFromS3")
        if not old_function:
            logger.error("[FixGen] moveAndRemoveFileFromS3 not found in file")
            return "", ""

        logger.info("[FixGen] Extracted function (%d chars)", len(old_function))

        prompt = f"""Fix this JavaScript function to handle the S3 NoSuchKey error gracefully.

CURRENT FUNCTION:
{old_function}

Apply ONLY this change: wrap the S3 operations in a try/catch block.
- If error.code === 'NoSuchKey': log a warning and return early
- For all other errors: re-throw so they surface normally
- Do NOT change the function signature or any logic outside the S3 calls

Return ONLY the complete fixed function — no explanation, no markdown fences."""

        new_function = await self._llm.complete(
            messages=[{"role": "user", "content": prompt}],
            system=(
                "You are a JavaScript engineer. "
                "Return ONLY the complete fixed function code, nothing else. "
                "No markdown, no backticks, no explanation."
            ),
        )

        # Strip any accidental markdown fences the model might add
        new_function = re.sub(r"^```(?:javascript|js)?\n?", "", new_function.strip())
        new_function = re.sub(r"\n?```$", "", new_function)

        return old_function, new_function.strip()
