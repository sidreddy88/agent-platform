"""
CICDAgent — monitors GitHub Actions, diagnoses failures, and suggests fixes.

Flow (driven by ReAct loop in BaseAgent):
  1. get_workflow_runs  → list recent runs, spot failures
  2. get_run_logs       → pull raw logs for a failed run
  3. analyze_failure    → classify failure type and extract root cause
  4. search_codebase    → find relevant code via RAG (optional, when context helps)
  5. suggest_fix        → produce an actionable fix recommendation

Failure types detected:
  TEST_FAILURE    — which test, which assertion failed
  BUILD_ERROR     — compilation / type errors
  DEPENDENCY      — missing package, version conflict, lockfile mismatch
  TIMEOUT         — job or step exceeded time limit
  FLAKY_TEST      — non-deterministic failure pattern
  UNKNOWN         — catch-all when logs are ambiguous
"""

import json
import re

from app.agents.base import AgentResult, BaseAgent
from app.services.github import GitHubError, GitHubService
from app.services.llm import LLMService
from app.services.rag import RAGService

# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

# Ordered — first match wins
_FAILURE_PATTERNS: list[tuple[str, str]] = [
    (r"(?i)(timed?\s*out|timeout|exceeded.*time\s*limit|cancell?ed.*timeout)", "TIMEOUT"),
    (r"(?i)(modulenotfounderror|cannot find module|no module named|importerror"
     r"|package.*not found|could not resolve|dependency.*conflict"
     r"|version.*conflict|lockfile|yarn\.lock|package-lock)", "DEPENDENCY"),
    (r"(?i)(assertionerror|assert.*failed|expected.*received|test.*fail"
     r"|FAIL\s+\w|● |✕ |✗ |FAILED tests/)", "TEST_FAILURE"),
    (r"(?i)(syntaxerror|typeerror.*is not|nameerror|compileerror"
     r"|error TS\d+|error\[E\d+\]|build failed|compilation failed"
     r"|cannot compile|type.*error)", "BUILD_ERROR"),
]

_FLAKY_HINTS = re.compile(
    r"(?i)(connection\s+reset|socket\s+hang\s+up|econnreset|network\s+error"
    r"|flaky|intermittent|retry\s+\d+|attempt\s+\d+\s+of)",
    re.IGNORECASE,
)


def _classify(logs: str) -> str:
    for pattern, failure_type in _FAILURE_PATTERNS:
        if re.search(pattern, logs, re.DOTALL):
            # Upgrade to FLAKY_TEST if network/retry signals co-occur
            if failure_type == "TEST_FAILURE" and _FLAKY_HINTS.search(logs):
                return "FLAKY_TEST"
            return failure_type
    return "UNKNOWN"


def _extract_error_snippet(logs: str, max_lines: int = 40) -> str:
    """Pull the most likely error block from raw logs."""
    lines = logs.splitlines()

    # Walk backwards to find the last ERROR / FAIL block
    error_start = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        if re.search(r"(?i)(error|failed|assert|exception|fatal)", lines[i]):
            error_start = i
            break

    snippet_lines = lines[max(0, error_start - 5): error_start + max_lines]
    return "\n".join(snippet_lines)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def get_workflow_runs(
    owner: str,
    repo: str,
    github: GitHubService,
    limit: int = 10,
) -> str:
    """List recent workflow runs with their status."""
    try:
        runs = await github.get_workflow_runs(owner, repo, limit=limit)
    except GitHubError as exc:
        return f"GitHub error: {exc}"

    if not runs:
        return "No workflow runs found."

    lines = ["Recent workflow runs:\n"]
    for r in runs:
        icon = {"success": "✓", "failure": "✗", "cancelled": "⊘"}.get(
            r["conclusion"] or r["status"], "○"
        )
        lines.append(
            f"  {icon} [{r['id']}] {r['name']} — {r['conclusion'] or r['status']}"
            f"  branch={r['branch']}  commit={r['commit_sha']}"
            f"  \"{r['commit_message']}\""
        )

    # Cache for downstream tools
    github._cached_runs = runs
    return "\n".join(lines)


async def get_run_logs(
    owner: str,
    repo: str,
    run_id: int,
    github: GitHubService,
) -> str:
    """Fetch logs for a failed workflow run."""
    try:
        logs = await github.get_run_logs(owner, repo, run_id)
    except GitHubError as exc:
        return f"GitHub error fetching logs: {exc}"

    github._cached_logs = {run_id: logs}

    # Return a truncated view — full logs can be huge
    lines = logs.splitlines()
    if len(lines) > 200:
        head = "\n".join(lines[:50])
        tail = "\n".join(lines[-150:])
        return f"{head}\n\n... [{len(lines) - 200} lines omitted] ...\n\n{tail}"
    return logs


async def analyze_failure(
    run_id: int,
    github: GitHubService,
    llm: LLMService,
) -> str:
    """Classify the failure type and extract root cause from cached logs."""
    cached = getattr(github, "_cached_logs", {})
    logs = cached.get(run_id)
    if not logs:
        return f"No cached logs for run {run_id}. Call get_run_logs first."

    failure_type = _classify(logs)
    snippet = _extract_error_snippet(logs)

    prompt = f"""You are a CI/CD expert analyzing a GitHub Actions failure.

FAILURE TYPE (auto-detected): {failure_type}

ERROR SNIPPET FROM LOGS:
{snippet}

Provide a structured analysis:

FAILURE_TYPE: {failure_type}

ROOT_CAUSE:
<1-3 sentences identifying the exact cause — be specific: which test, which file, which package, which type error>

KEY_ERROR:
<The single most important error line or message>

AFFECTED_FILES:
<Comma-separated list of file paths mentioned in the error, or "unknown">

SEARCH_QUERY:
<A short query (5-10 words) to search the codebase for relevant code that might need fixing>

Be precise. Avoid vague statements like "there was an error"."""

    analysis = await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system="You are a senior DevOps engineer diagnosing CI pipeline failures.",
    )

    # Cache for suggest_fix
    github._cached_analysis = {"run_id": run_id, "failure_type": failure_type, "text": analysis}
    return analysis


async def search_codebase(query: str, rag: RAGService | None) -> str:
    """Search the indexed codebase for code relevant to the failure."""
    if rag is None:
        return "RAG not configured — codebase search unavailable."
    try:
        chunks = await rag.search(query, n_results=4)
    except Exception as exc:
        return f"RAG search error: {exc}"

    if not chunks:
        return "No relevant code found (codebase may not be indexed yet)."

    parts = []
    for c in chunks:
        parts.append(
            f"--- {c.file_path}:{c.start_line}-{c.end_line} (score={c.score}) ---\n"
            f"{c.content[:400]}"
        )
    return "\n\n".join(parts)


async def suggest_fix(
    run_id: int,
    github: GitHubService,
    llm: LLMService,
    codebase_context: str = "",
) -> str:
    """Generate a concrete, actionable fix recommendation."""
    analysis_cache = getattr(github, "_cached_analysis", {})
    if not analysis_cache or analysis_cache.get("run_id") != run_id:
        return "No analysis cached. Call analyze_failure first."

    failure_type = analysis_cache["failure_type"]
    analysis = analysis_cache["text"]

    type_guidance = {
        "TEST_FAILURE": (
            "Focus on: what the test expects vs what it received, "
            "whether the logic or the test needs fixing, and the exact code change."
        ),
        "BUILD_ERROR": (
            "Focus on: the type/syntax error location, the correct type or syntax, "
            "and the exact line to change."
        ),
        "DEPENDENCY": (
            "Focus on: the missing/conflicting package, the correct version to pin, "
            "and the exact command to run (pip install / npm install / etc.)."
        ),
        "TIMEOUT": (
            "Focus on: which step timed out, why it might be slow, "
            "and whether to optimize the code or increase the timeout."
        ),
        "FLAKY_TEST": (
            "Focus on: why this test is non-deterministic (timing, network, shared state), "
            "and how to make it reliable (mock, retry logic, isolation)."
        ),
        "UNKNOWN": (
            "Focus on: the most likely cause based on any available error signals "
            "and provide a general debugging approach."
        ),
    }

    prompt = f"""You are a senior engineer providing a fix recommendation for a CI failure.

FAILURE TYPE: {failure_type}
{type_guidance.get(failure_type, "")}

FAILURE ANALYSIS:
{analysis}

RELEVANT CODEBASE CONTEXT:
{codebase_context or "(none retrieved)"}

Write a fix recommendation in EXACTLY this format:

# CI Fix: {failure_type}

## What Went Wrong
<2-3 sentences explaining the failure clearly>

## Recommended Fix

### Immediate Action
<The single most important thing to do — be specific with file names, commands, or code>

### Code Change (if applicable)
```
<Show the exact before/after code change, or the command to run>
```

### Verification
<How to confirm the fix worked — what to check in the next CI run>

## Prevention
<1-2 sentences on how to prevent this class of failure in the future>

## Confidence
<HIGH / MEDIUM / LOW> — <one sentence explaining why>"""

    return await llm.complete(
        messages=[{"role": "user", "content": prompt}],
        system="You are a senior engineer writing precise, actionable CI fix recommendations.",
    )


# ---------------------------------------------------------------------------
# CICDAgent
# ---------------------------------------------------------------------------

class CICDAgent(BaseAgent):
    """
    Monitors GitHub Actions, diagnoses CI/CD failures, and suggests fixes.

    Usage:
        agent = CICDAgent()
        result = await agent.run('{"owner": "acme", "repo": "backend"}')
        print(result.answer)

    Input JSON fields:
        owner    - GitHub repo owner
        repo     - Repository name
        run_id   - (optional) specific run to analyze; if omitted, agent picks
                   the most recent failure automatically
    """

    def __init__(
        self,
        github: GitHubService | None = None,
        rag: RAGService | None = None,
    ) -> None:
        super().__init__()
        self._github = github or GitHubService()
        self._rag = rag  # RAG is optional — agent gracefully skips if None
        self._register_tools()

    def _register_tools(self) -> None:
        llm = self._llm
        gh = self._github
        rag = self._rag

        async def _get_workflow_runs(owner: str, repo: str, limit: int = 10) -> str:
            return await get_workflow_runs(owner, repo, gh, limit=limit)

        async def _get_run_logs(owner: str, repo: str, run_id: int) -> str:
            return await get_run_logs(owner, repo, run_id, gh)

        async def _analyze_failure(run_id: int) -> str:
            return await analyze_failure(run_id, gh, llm)

        async def _search_codebase(query: str) -> str:
            if rag is None:
                return "RAG not configured — codebase search unavailable."
            return await search_codebase(query, rag)

        async def _suggest_fix(run_id: int, codebase_context: str = "") -> str:
            return await suggest_fix(run_id, gh, llm, codebase_context)

        self.register_tool(
            "get_workflow_runs",
            _get_workflow_runs,
            (
                "List recent GitHub Actions workflow runs with their status. "
                "Input: {owner: string, repo: string, limit: integer (optional, default 10)}"
            ),
        )
        self.register_tool(
            "get_run_logs",
            _get_run_logs,
            (
                "Fetch the logs for a specific workflow run (focuses on failed jobs). "
                "Input: {owner: string, repo: string, run_id: integer}"
            ),
        )
        self.register_tool(
            "analyze_failure",
            _analyze_failure,
            (
                "Classify the failure type (TEST_FAILURE, BUILD_ERROR, DEPENDENCY, "
                "TIMEOUT, FLAKY_TEST, UNKNOWN) and extract the root cause from cached logs. "
                "Must call get_run_logs first. "
                "Input: {run_id: integer}"
            ),
        )
        self.register_tool(
            "search_codebase",
            _search_codebase,
            (
                "Search the indexed codebase for code relevant to the failure. "
                "Use the SEARCH_QUERY from analyze_failure output. "
                "Input: {query: string}"
            ),
        )
        self.register_tool(
            "suggest_fix",
            _suggest_fix,
            (
                "Generate a concrete fix recommendation using the failure analysis "
                "and optional codebase context. Must call analyze_failure first. "
                "Input: {run_id: integer, codebase_context: string (optional)}"
            ),
        )

    async def run(self, user_input: str) -> AgentResult:
        """
        Run the CI/CD agent.

        user_input can be JSON:
            {"owner": "acme", "repo": "backend"}
            {"owner": "acme", "repo": "backend", "run_id": 12345678}
        or plain text:
            "Check CI failures for acme/backend"
        """
        try:
            params = json.loads(user_input)
            owner = params["owner"]
            repo = params["repo"]
            run_id_hint = f" Focus on run ID {params['run_id']}." if "run_id" in params else ""

            prompt = (
                f"Analyze CI/CD failures for {owner}/{repo}.{run_id_hint} "
                "First list recent workflow runs to identify failures. "
                "Then fetch logs for the most recent failed run (or the specified run). "
                "Analyze the failure to determine its type and root cause. "
                "Search the codebase if the analysis suggests specific files or code to look at. "
                "Finally suggest a concrete fix. "
                "Produce a complete CI failure report as your final answer.\n\n"
                "MANDATORY CONSTRAINTS:\n"
                "- Call get_workflow_runs exactly once as your first tool call, then move on.\n"
                "- You MUST call get_run_logs and analyze_failure before writing your Answer.\n"
                "- Never produce a CI failure report from memory — all data must come from tool results.\n"
                "- If a specific run_id is not found, say so clearly and analyze the most recent failure instead."
            )
        except (json.JSONDecodeError, KeyError):
            prompt = (
                user_input + " "
                "First list recent workflow runs. Fetch logs for the most recent failure. "
                "Analyze the failure type and root cause. Search the codebase if helpful. "
                "Suggest a concrete fix as your final answer.\n\n"
                "MANDATORY CONSTRAINTS: You MUST call get_workflow_runs first, then get_run_logs "
                "and analyze_failure, before writing your Answer. Never produce a report from memory."
            )

        return await super().run(prompt)
