"""
CodeReviewAgent — reviews a GitHub pull request and produces a structured report.

Flow (driven by ReAct loop in BaseAgent):
  1. fetch_pr        → retrieve PR metadata and list of changed files with diffs
  2. analyze_file    → deep-dive analysis on a single file's diff
  3. generate_review → assemble the final structured review and post it to GitHub
"""

import json

from app.agents.base import AgentResult, BaseAgent
from app.services.github import FileDiff, GitHubError, GitHubService, PRDetails
from app.services.llm import LLMService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_pr(pr: PRDetails, files: list[FileDiff]) -> str:
    changed = "\n".join(
        f"  - {f.filename} [{f.status}] +{f.additions}/-{f.deletions}"
        for f in files
    )
    return (
        f"PR #{pr.number}: {pr.title}\n"
        f"Author : {pr.author}\n"
        f"Branches: {pr.base_branch} ← {pr.head_branch}\n"
        f"Description: {pr.description or '(none)'}\n\n"
        f"Changed files ({len(files)}):\n{changed}"
    )


def _format_file_diff(f: FileDiff) -> str:
    patch = f.patch or "(binary or oversized file — no patch available)"
    return (
        f"File   : {f.filename}\n"
        f"Status : {f.status}  +{f.additions}/-{f.deletions}\n\n"
        f"Diff:\n{patch}"
    )


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def fetch_pr(
    owner: str,
    repo: str,
    pr_number: int,
    github: GitHubService,
) -> str:
    """Fetch PR metadata and diff summary from GitHub."""
    try:
        pr = await github.get_pr(owner, repo, pr_number)
        files = await github.get_pr_diff(owner, repo, pr_number)
    except GitHubError as exc:
        return f"GitHub error: {exc}"

    # Store on service so other tools can reuse without re-fetching
    github._cached_pr = pr
    github._cached_files = {f.filename: f for f in files}

    return _format_pr(pr, files)


async def analyze_file(
    filename: str,
    github: GitHubService,
    llm: LLMService,
) -> str:
    """Deep-dive analysis of a single changed file's diff."""
    files: dict[str, FileDiff] = getattr(github, "_cached_files", {})
    if filename not in files:
        available = ", ".join(files.keys()) or "none (call fetch_pr first)"
        return f"File '{filename}' not found in cached diff. Available: {available}"

    diff_text = _format_file_diff(files[filename])

    prompt = f"""You are a senior code reviewer. Analyze this file diff for issues.

{diff_text}

Check for:
1. BUGS       — logic errors, off-by-one, null/undefined dereferences, wrong conditions,
                unhandled exceptions, incorrect error propagation
2. SECURITY   — SQL injection, XSS, command injection, hardcoded secrets or tokens,
                insecure deserialization, path traversal, missing auth checks
3. PERFORMANCE — N+1 queries, O(n²) loops, missing indexes hinted by the code,
                 unnecessary allocations, synchronous blocking in async context
4. TESTING    — missing tests for new logic, untested edge cases, missing error path tests

For each issue found output exactly:
ISSUE | <severity: CRITICAL/HIGH/MEDIUM/LOW> | line <N or range> | <category> | <concise description>

If a line number cannot be determined from the diff, use line 0.
If no issues are found in a category, omit it.
End with a one-sentence summary of this file's overall quality."""

    return await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system="You are a security-conscious senior engineer doing a thorough code review.",
    )


async def generate_review(
    owner: str,
    repo: str,
    pr_number: int,
    file_analyses: str,
    github: GitHubService,
    llm: LLMService,
    post_to_github: bool = False,
) -> str:
    """Assemble a structured review and optionally post it back to the PR."""
    pr: PRDetails | None = getattr(github, "_cached_pr", None)
    if pr is None:
        return "Error: PR not fetched yet. Call fetch_pr first."

    files: dict[str, FileDiff] = getattr(github, "_cached_files", {})
    file_summary = "\n".join(
        f"  {f.filename} [{f.status}] +{f.additions}/-{f.deletions}"
        for f in files.values()
    )

    prompt = f"""You are a staff engineer writing the final code review for a pull request.

PR #{pr.number}: {pr.title}
Author : {pr.author}
Branches: {pr.base_branch} ← {pr.head_branch}
Description: {pr.description or '(none)'}

Changed files:
{file_summary}

Per-file analysis results:
{file_analyses}

Write the review in EXACTLY this format:

# Code Review: PR #{pr.number} — {pr.title}

## Summary of Changes
<2-4 sentences describing what this PR does overall>

## Issues Found

### Critical
<bullet per issue: `filename:line` — description. Write "None" if no critical issues.>

### High
<bullet per issue: `filename:line` — description. Write "None" if no high issues.>

### Medium
<bullet per issue: `filename:line` — description. Write "None" if no medium issues.>

### Low
<bullet per issue: `filename:line` — description. Write "None" if no low issues.>

## Security Assessment
<1-3 sentences on security posture of this change>

## Performance Assessment
<1-3 sentences on performance implications>

## Testing Gaps
<bullet list of missing tests, or "Coverage appears adequate.">

## Recommendation
<One of: APPROVE | REQUEST_CHANGES | NEEDS_DISCUSSION>

**Rationale:** <1-2 sentences explaining the recommendation>

Be specific. Reference actual filenames and line numbers from the analysis."""

    review_text = await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system="You are a staff engineer writing an actionable, fair code review.",
    )

    if post_to_github:
        try:
            event_map = {
                "APPROVE": "APPROVE",
                "REQUEST_CHANGES": "REQUEST_CHANGES",
                "NEEDS_DISCUSSION": "COMMENT",
            }
            # Determine event from recommendation line
            event = "COMMENT"
            for key, val in event_map.items():
                if key in review_text:
                    event = val
                    break

            await github.post_pr_review(owner, repo, pr_number, review_text, event=event)
            return review_text + "\n\n---\n✓ Review posted to GitHub."
        except GitHubError as exc:
            return review_text + f"\n\n---\nWarning: failed to post to GitHub: {exc}"

    return review_text


# ---------------------------------------------------------------------------
# CodeReviewAgent
# ---------------------------------------------------------------------------

class CodeReviewAgent(BaseAgent):
    """
    Reviews a GitHub pull request and produces a structured code review.

    Usage:
        agent = CodeReviewAgent()
        result = await agent.run(
            '{"owner": "acme", "repo": "backend", "pr_number": 42, "post_to_github": true}'
        )
        print(result.answer)

    Input JSON fields:
        owner          - GitHub repo owner (user or org)
        repo           - Repository name
        pr_number      - Pull request number
        post_to_github - (optional, default false) post the review back to the PR
    """

    def __init__(self, github: GitHubService | None = None) -> None:
        super().__init__()
        self._github = github or GitHubService()
        self._register_tools()

    def _register_tools(self) -> None:
        llm = self._llm
        gh = self._github

        async def _fetch_pr(owner: str, repo: str, pr_number: int) -> str:
            return await fetch_pr(owner, repo, pr_number, gh)

        async def _analyze_file(filename: str) -> str:
            return await analyze_file(filename, gh, llm)

        async def _generate_review(
            owner: str,
            repo: str,
            pr_number: int,
            file_analyses: str,
            post_to_github: bool = False,
        ) -> str:
            return await generate_review(
                owner, repo, pr_number, file_analyses, gh, llm, post_to_github
            )

        self.register_tool(
            "fetch_pr",
            _fetch_pr,
            (
                "Fetch PR metadata and changed-file summary from GitHub. "
                "Input: {owner: string, repo: string, pr_number: integer}"
            ),
        )
        self.register_tool(
            "analyze_file",
            _analyze_file,
            (
                "Deep-dive analysis of a single changed file's diff — checks for bugs, "
                "security issues, performance problems, and testing gaps. "
                "Must call fetch_pr first. "
                "Input: {filename: string}"
            ),
        )
        self.register_tool(
            "generate_review",
            _generate_review,
            (
                "Assemble the final structured review from all per-file analyses and "
                "optionally post it to the GitHub PR. Call after all files are analyzed. "
                "Input: {owner: string, repo: string, pr_number: integer, "
                "file_analyses: string, post_to_github: boolean}"
            ),
        )

    async def run(self, user_input: str) -> AgentResult:
        """
        Run the code review agent.

        user_input should be a JSON string:
            {"owner": "...", "repo": "...", "pr_number": 42}
        or plain text like:
            "Review PR #42 in acme/backend"
        """
        # Normalise plain-text shorthand into a richer prompt for the ReAct loop
        try:
            params = json.loads(user_input)
            prompt = (
                f"Review pull request #{params['pr_number']} in "
                f"{params['owner']}/{params['repo']}. "
                f"{'Post the review to GitHub when done.' if params.get('post_to_github') else 'Do not post to GitHub.'} "
                "Fetch the PR first, then analyze every changed file individually, "
                "then generate the final structured review."
            )
        except (json.JSONDecodeError, KeyError):
            prompt = (
                user_input + " "
                "Fetch the PR first, then analyze every changed file individually, "
                "then generate the final structured review."
            )

        return await super().run(prompt)
