"""
CodeReviewAgent — reviews a GitHub pull request and produces a structured report.

Flow (direct sequential calls — no ReAct loop):
  1. fetch_pr        → retrieve PR metadata and list of changed files with diffs
  2. analyze_file    → deep-dive analysis on each changed file's diff
  3. generate_review → assemble the final structured review and post it to GitHub
"""

import json
import logging
from pathlib import Path

from app.agents.base import AgentResult, BaseAgent
from app.services.github import FileDiff, GitHubError, GitHubService, PRDetails
from app.services.llm import LLMService

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_rag_query(filename: str, patch: str) -> str:
    """Build a search query from the filename stem + function/class names in the diff."""
    terms = [Path(filename).stem.replace("_", " ").replace("-", " ")]
    for line in (patch or "").splitlines():
        stripped = line.lstrip("+-").strip()
        if stripped.startswith(("def ", "async def ", "class ", "function ")):
            name = stripped.split("(")[0].split(" ")[-1]
            if name and name not in terms:
                terms.append(name)
        if len(terms) >= 6:
            break
    return " ".join(terms)


def _format_rag_context(chunks) -> str:
    """Format codebase RAG results as a context block for the LLM prompt."""
    lines = ["Related codebase context (files semantically related to this diff):"]
    for chunk in chunks:
        lines.append(f"\n--- {chunk.file_path} (lines {chunk.start_line}–{chunk.end_line}, score={chunk.score:.2f}) ---")
        lines.append(chunk.content[:600])  # cap per chunk to avoid prompt bloat
    return "\n".join(lines)

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
    rag=None,
) -> str:
    """Deep-dive analysis of a single changed file's diff."""
    files: dict[str, FileDiff] = getattr(github, "_cached_files", {})
    if filename not in files:
        available = ", ".join(files.keys()) or "none (call fetch_pr first)"
        return f"File '{filename}' not found in cached diff. Available: {available}"

    diff = files[filename]
    diff_text = _format_file_diff(diff)

    rag_section = ""
    if rag is not None:
        try:
            if rag._collection.count() > 0:
                query = _extract_rag_query(filename, diff.patch or "")
                chunks = await rag.search(query, n_results=4)
                # exclude chunks from the file being reviewed — already in the diff
                chunks = [c for c in chunks if c.file_path != filename][:3]
                if chunks:
                    rag_section = "\n\n" + _format_rag_context(chunks)
        except Exception as exc:
            logger.debug("[CodeReview] RAG context skipped: %s", exc)

    ai_fix_warning = ""
    if "ai-generated-fix" in (diff_text.lower()) or "awaiting-review" in diff_text.lower():
        ai_fix_warning = """
⚠️  THIS IS AN AI-GENERATED FIX. Be especially skeptical.
Key questions to answer:
- Does the fix address the ROOT CAUSE or does it just suppress/convert the error?
- Would a human engineer write it this way, or is it a workaround?
- Does it handle ALL invalid inputs, not just the specific bad value that triggered the error?
"""

    prompt = f"""You are a senior code reviewer. Analyze this file diff for issues.

{diff_text}{rag_section}{ai_fix_warning}

Check for:
1. CORRECTNESS — does the fix address the actual root cause, or does it just suppress the symptom?
                 (e.g. converting invalid input instead of rejecting it is a symptom fix)
                 SYMPTOM-FIX RED FLAGS — treat any of these as CRITICAL unless there is a strong reason:
                 a) A null/undefined guard (if (x && x.y), x?.y, x ?? default, try/catch) is added
                    at or near the crash line without also fixing the function that produces x.
                    The correct fix is in the producer, not the consumer.
                 b) An LLM/API/DB response is guarded with optional chaining at the access site
                    instead of validating/normalising it in the function that makes the call.
                 c) The same crash site was patched in a previous PR (regression) — if the error
                    recurs at the same line, the upstream source was never fixed.
2. BUGS       — logic errors, off-by-one, null/undefined dereferences, wrong conditions,
                unhandled exceptions, incorrect error propagation
3. SECURITY   — SQL injection, XSS, command injection, hardcoded secrets or tokens,
                insecure deserialization, path traversal, missing auth checks
4. PERFORMANCE — N+1 queries, O(n²) loops, missing indexes hinted by the code,
                 unnecessary allocations, synchronous blocking in async context
5. CROSS-FILE — if related codebase context is provided above: duplicate logic that already
                exists elsewhere, callers that may break due to signature changes, patterns
                that contradict how the rest of the codebase handles the same concern

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

Be specific. Reference actual filenames and line numbers from the analysis.

IMPORTANT: Your review must be grounded solely in the per-file analysis above.
- Do NOT raise issues not evidenced in the analysis.
- Do NOT claim something is missing because the PR description mentions it — the description states intent, not ground truth. If the per-file analysis did not flag it as missing, it is not missing.
- Do NOT invent issues, guess at absent code, or speculate beyond what the diff shows."""

    review_text = await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system="You are a staff engineer writing an actionable, fair code review.",
    )

    if post_to_github:
        try:
            # Always use COMMENT — APPROVE/REQUEST_CHANGES are rejected by GitHub
            # when the reviewer is the same user who opened the PR (422 Unprocessable Entity).
            # The recommendation is communicated in the review body text instead.
            await github.post_pr_review(owner, repo, pr_number, review_text, event="COMMENT")
            logger.info("[CodeReview] Posted review to PR #%d", pr_number)
            return review_text + "\n\n---\n✓ Review posted to GitHub."
        except GitHubError as exc:
            logger.error("[CodeReview] Failed to post review to PR #%d: %s", pr_number, exc)
            return review_text + f"\n\n---\n✗ Failed to post review to GitHub: {exc}"

    return review_text


# ---------------------------------------------------------------------------
# CodeReviewAgent
# ---------------------------------------------------------------------------

class CodeReviewAgent(BaseAgent):
    """
    Reviews a GitHub pull request and produces a structured code review.

    Uses direct sequential calls instead of a ReAct loop — the steps are
    deterministic (fetch → analyze each file → generate review) so an agent
    loop adds no value and allows the model to skip tool calls entirely.

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
        self._rag = None
        try:
            from app.services.rag import RAGService
            self._rag = RAGService()
        except Exception:
            pass  # no OpenAI key or ChromaDB — reviews still work, just without codebase context

    async def run(self, user_input: str) -> AgentResult:
        """
        Run the code review pipeline directly (no ReAct loop).

        user_input: JSON string with owner, repo, pr_number, post_to_github.
        """
        try:
            params = json.loads(user_input)
            owner = params["owner"]
            repo = params["repo"]
            pr_number = int(params["pr_number"])
            post = bool(params.get("post_to_github", False))
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            return AgentResult(answer=f"Invalid input: {exc}", steps=[], iterations=0)

        # ── 1. Fetch PR metadata + diff ────────────────────────────────
        pr_summary = await fetch_pr(owner, repo, pr_number, self._github)
        if pr_summary.startswith("GitHub error:"):
            return AgentResult(answer=pr_summary, steps=[], iterations=1)

        # ── 2. Analyze each changed file ───────────────────────────────
        files: dict = getattr(self._github, "_cached_files", {})
        analysis_parts: list[str] = []
        for filename in files:
            analysis = await analyze_file(filename, self._github, self._llm, self._rag)
            analysis_parts.append(f"### {filename}\n{analysis}")

        file_analyses = "\n\n".join(analysis_parts) if analysis_parts else "No files to analyze."

        # ── 3. Generate review and optionally post to GitHub ───────────
        review = await generate_review(
            owner, repo, pr_number, file_analyses, self._github, self._llm,
            post_to_github=post,
        )

        return AgentResult(answer=review, steps=[], iterations=3)
