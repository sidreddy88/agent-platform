"""
Tests for CodeReviewAgent.

Two modes:
  - Unit tests (default): all GitHub + LLM calls are mocked — no network, no API keys needed.
  - Live test (opt-in):   set env vars GITHUB_TOKEN, TEST_OWNER, TEST_REPO, TEST_PR_NUMBER
                          then run:  pytest tests/test_code_review_agent.py -m live -s

Run unit tests:
    pytest tests/test_code_review_agent.py -v
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.code_review import (
    CodeReviewAgent,
    analyze_file,
    fetch_pr,
    generate_review,
)
from app.services.github import FileDiff, GitHubError, GitHubService, PRDetails

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_PR = PRDetails(
    number=42,
    title="Add user authentication",
    description="Implements JWT-based login flow",
    author="dev-user",
    head_branch="feature/auth",
    base_branch="main",
    head_sha="abc123def456",
)

FAKE_FILES = [
    FileDiff(
        filename="app/auth.py",
        status="added",
        additions=80,
        deletions=0,
        patch=(
            "@@ -0,0 +1,10 @@\n"
            "+def login(username, password):\n"
            "+    query = f\"SELECT * FROM users WHERE username='{username}'\"\n"
            "+    user = db.execute(query)\n"
            "+    if user and user.password == password:\n"
            "+        return generate_token(user)\n"
        ),
    ),
    FileDiff(
        filename="app/utils.py",
        status="modified",
        additions=5,
        deletions=2,
        patch=(
            "@@ -10,7 +10,10 @@\n"
            "-def hash_password(pw):\n"
            "+def hash_password(pw: str) -> str:\n"
            "+    import hashlib\n"
            "+    return hashlib.md5(pw.encode()).hexdigest()\n"
        ),
    ),
]


def make_github_mock() -> MagicMock:
    """Return a GitHubService mock pre-loaded with fake PR + files."""
    gh = MagicMock(spec=GitHubService)
    gh.get_pr = AsyncMock(return_value=FAKE_PR)
    gh.get_pr_diff = AsyncMock(return_value=FAKE_FILES)
    gh.post_pr_review = AsyncMock(return_value={"id": 1, "state": "COMMENTED"})
    gh._cached_pr = None
    gh._cached_files = {}
    return gh


def make_llm_mock(response: str = "Mocked LLM response") -> MagicMock:
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=response)
    return llm


# ---------------------------------------------------------------------------
# Unit tests — fetch_pr tool
# ---------------------------------------------------------------------------

class TestFetchPR:
    @pytest.mark.asyncio
    async def test_returns_pr_summary(self):
        gh = make_github_mock()
        result = await fetch_pr("acme", "backend", 42, gh)

        assert "PR #42" in result
        assert "Add user authentication" in result
        assert "dev-user" in result
        assert "app/auth.py" in result
        assert "app/utils.py" in result

    @pytest.mark.asyncio
    async def test_caches_pr_and_files(self):
        gh = make_github_mock()
        await fetch_pr("acme", "backend", 42, gh)

        assert gh._cached_pr == FAKE_PR
        assert "app/auth.py" in gh._cached_files
        assert "app/utils.py" in gh._cached_files

    @pytest.mark.asyncio
    async def test_handles_github_error(self):
        gh = make_github_mock()
        gh.get_pr = AsyncMock(side_effect=GitHubError(404, "Not Found"))

        result = await fetch_pr("acme", "backend", 999, gh)
        assert "GitHub error" in result
        assert "404" in result

    @pytest.mark.asyncio
    async def test_handles_rate_limit(self):
        gh = make_github_mock()
        gh.get_pr = AsyncMock(side_effect=GitHubError(429, "Rate limit exceeded. Resets at epoch 9999."))

        result = await fetch_pr("acme", "backend", 42, gh)
        assert "GitHub error" in result
        assert "429" in result


# ---------------------------------------------------------------------------
# Unit tests — analyze_file tool
# ---------------------------------------------------------------------------

class TestAnalyzeFile:
    @pytest.mark.asyncio
    async def test_analyzes_cached_file(self):
        gh = make_github_mock()
        gh._cached_files = {f.filename: f for f in FAKE_FILES}
        llm = make_llm_mock("ISSUE | CRITICAL | line 2 | SECURITY | SQL injection vulnerability")

        result = await analyze_file("app/auth.py", gh, llm)

        assert result == "ISSUE | CRITICAL | line 2 | SECURITY | SQL injection vulnerability"
        llm.complete.assert_called_once()
        # The prompt sent to LLM should include the diff
        prompt_sent = llm.complete.call_args[1]["messages"][0]["content"]
        assert "app/auth.py" in prompt_sent
        assert "SELECT" in prompt_sent

    @pytest.mark.asyncio
    async def test_missing_file_returns_error(self):
        gh = make_github_mock()
        gh._cached_files = {}

        llm = make_llm_mock()
        result = await analyze_file("nonexistent.py", gh, llm)

        assert "not found" in result
        assert "fetch_pr first" in result
        llm.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_binary_file(self):
        binary_file = FileDiff(
            filename="assets/logo.png",
            status="added",
            additions=0,
            deletions=0,
            patch=None,
        )
        gh = make_github_mock()
        gh._cached_files = {"assets/logo.png": binary_file}
        llm = make_llm_mock("No code issues in binary file.")

        result = await analyze_file("assets/logo.png", gh, llm)
        assert result == "No code issues in binary file."

    @pytest.mark.asyncio
    async def test_default_review_kind_keeps_symptom_fix_criteria(self):
        """review_kind defaults to "fix" — existing FixGenerationAgent PRs must keep
        being reviewed against root-cause/symptom-fix criteria, unchanged."""
        gh = make_github_mock()
        gh._cached_files = {f.filename: f for f in FAKE_FILES}
        llm = make_llm_mock("LOOKS CORRECT")

        await analyze_file("app/auth.py", gh, llm)

        prompt_sent = llm.complete.call_args[1]["messages"][0]["content"]
        assert "does the fix address the actual root cause" in prompt_sent
        assert "SYMPTOM-FIX RED FLAGS" in prompt_sent
        assert "OBSERVABILITY ADDITION" not in prompt_sent

    @pytest.mark.asyncio
    async def test_clarity_review_kind_drops_symptom_fix_criteria(self):
        """The real gap this closes: an ErrorClarityAgent PR isn't fixing a bug, so
        root-cause/symptom-fix criteria don't apply and shouldn't be asked."""
        gh = make_github_mock()
        gh._cached_files = {f.filename: f for f in FAKE_FILES}
        llm = make_llm_mock("LOOKS CORRECT")

        await analyze_file("app/auth.py", gh, llm, review_kind="clarity")

        prompt_sent = llm.complete.call_args[1]["messages"][0]["content"]
        assert "does the fix address the actual root cause" not in prompt_sent
        assert "SYMPTOM-FIX RED FLAGS" not in prompt_sent
        assert "there is no bug being fixed and no root cause to address" in prompt_sent

    @pytest.mark.asyncio
    async def test_clarity_review_kind_asks_about_secrets_and_pii(self):
        """The check that actually matters for a logging addition: does it leak
        secrets/PII into logs — not present at all in the "fix" framing."""
        gh = make_github_mock()
        gh._cached_files = {f.filename: f for f in FAKE_FILES}
        llm = make_llm_mock("LOOKS CORRECT")

        await analyze_file("app/auth.py", gh, llm, review_kind="clarity")

        prompt_sent = llm.complete.call_args[1]["messages"][0]["content"]
        assert "secrets" in prompt_sent.lower()
        assert "pii" in prompt_sent.lower()


# ---------------------------------------------------------------------------
# Unit tests — generate_review tool
# ---------------------------------------------------------------------------

FAKE_REVIEW = """\
# Code Review: PR #42 — Add user authentication

## Summary of Changes
This PR adds JWT-based authentication.

## Issues Found

### Critical
- `app/auth.py:2` — SQL injection via string interpolation

### High
- `app/utils.py:3` — MD5 is not a secure password hashing algorithm

### Medium
None

### Low
None

## Security Assessment
Significant security issues found.

## Performance Assessment
No performance concerns.

## Testing Gaps
- Missing unit tests for login()

## Recommendation
REQUEST_CHANGES

**Rationale:** Critical SQL injection and weak hashing must be fixed before merge.
"""


class TestGenerateReview:
    @pytest.mark.asyncio
    async def test_returns_structured_review(self):
        gh = make_github_mock()
        gh._cached_pr = FAKE_PR
        gh._cached_files = {f.filename: f for f in FAKE_FILES}
        llm = make_llm_mock(FAKE_REVIEW)

        result = await generate_review(
            "acme", "backend", 42, "file analyses here", gh, llm, post_to_github=False
        )

        assert "Code Review" in result
        assert "REQUEST_CHANGES" in result
        gh.post_pr_review.assert_not_called()

    @pytest.mark.asyncio
    async def test_posts_to_github_when_flag_set(self):
        gh = make_github_mock()
        gh._cached_pr = FAKE_PR
        gh._cached_files = {f.filename: f for f in FAKE_FILES}
        llm = make_llm_mock(FAKE_REVIEW)

        result = await generate_review(
            "acme", "backend", 42, "file analyses here", gh, llm, post_to_github=True
        )

        gh.post_pr_review.assert_called_once()
        call_args = gh.post_pr_review.call_args
        # Always COMMENT — APPROVE/REQUEST_CHANGES are rejected by GitHub with 422
        # when the reviewer is the same user who opened the PR.
        event = call_args.kwargs.get("event") or call_args.args[4]
        assert event == "COMMENT"
        assert "posted to GitHub" in result

    @pytest.mark.asyncio
    async def test_github_post_failure_does_not_lose_review(self):
        gh = make_github_mock()
        gh._cached_pr = FAKE_PR
        gh._cached_files = {f.filename: f for f in FAKE_FILES}
        gh.post_pr_review = AsyncMock(side_effect=GitHubError(403, "Forbidden"))
        llm = make_llm_mock(FAKE_REVIEW)

        result = await generate_review(
            "acme", "backend", 42, "analyses", gh, llm, post_to_github=True
        )

        # Review text is still returned even if posting fails
        assert "Code Review" in result
        assert "Failed to post review to GitHub" in result

    @pytest.mark.asyncio
    async def test_no_cached_pr_returns_error(self):
        gh = make_github_mock()
        gh._cached_pr = None
        llm = make_llm_mock()

        result = await generate_review("acme", "backend", 42, "", gh, llm)
        assert "fetch_pr first" in result
        llm.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_clarity_review_kind_adds_context_note(self):
        gh = make_github_mock()
        gh._cached_pr = FAKE_PR
        gh._cached_files = {f.filename: f for f in FAKE_FILES}
        llm = make_llm_mock(FAKE_REVIEW)

        await generate_review(
            "acme", "backend", 42, "file analyses here", gh, llm,
            post_to_github=False, review_kind="clarity",
        )

        prompt_sent = llm.complete.call_args[1]["messages"][0]["content"]
        assert "observability addition from ErrorClarityAgent" in prompt_sent

    @pytest.mark.asyncio
    async def test_default_review_kind_omits_clarity_context_note(self):
        gh = make_github_mock()
        gh._cached_pr = FAKE_PR
        gh._cached_files = {f.filename: f for f in FAKE_FILES}
        llm = make_llm_mock(FAKE_REVIEW)

        await generate_review(
            "acme", "backend", 42, "file analyses here", gh, llm, post_to_github=False,
        )

        prompt_sent = llm.complete.call_args[1]["messages"][0]["content"]
        assert "observability addition from ErrorClarityAgent" not in prompt_sent


# ---------------------------------------------------------------------------
# Unit tests — CodeReviewAgent (full agent with mocked LLM + GitHub)
# ---------------------------------------------------------------------------

AGENT_FINAL_ANSWER = f"Answer: {FAKE_REVIEW}"

AGENT_REACT_SEQUENCE = [
    # Step 1: fetch the PR
    (
        "Thought: I need to fetch the PR details first.\n"
        'Action: fetch_pr\n'
        'Action Input: {"owner": "acme", "repo": "backend", "pr_number": 42}'
    ),
    # Step 2: analyze first file
    (
        "Thought: Now I'll analyze app/auth.py.\n"
        'Action: analyze_file\n'
        'Action Input: {"filename": "app/auth.py"}'
    ),
    # Step 3: analyze second file
    (
        "Thought: Now I'll analyze app/utils.py.\n"
        'Action: analyze_file\n'
        'Action Input: {"filename": "app/utils.py"}'
    ),
    # Step 4: generate the review
    (
        "Thought: I have all analyses. Generating the final review.\n"
        'Action: generate_review\n'
        'Action Input: {"owner": "acme", "repo": "backend", "pr_number": 42, '
        '"file_analyses": "analysis results", "post_to_github": false}'
    ),
    # Step 5: final answer
    f"Thought: Review is complete.\n{AGENT_FINAL_ANSWER}",
]


class TestCodeReviewAgent:
    def _make_agent(self, llm_responses: list[str]) -> tuple[CodeReviewAgent, MagicMock]:
        gh = make_github_mock()
        # Wire up caches as the real fetch_pr tool would
        gh.get_pr = AsyncMock(return_value=FAKE_PR)
        gh.get_pr_diff = AsyncMock(return_value=FAKE_FILES)

        agent = CodeReviewAgent(github=gh)

        # Replace the LLM with one that returns scripted responses in order
        llm_mock = MagicMock()
        llm_mock.complete = AsyncMock(side_effect=llm_responses)
        agent._llm = llm_mock

        # Also patch the analyze_file / generate_review tool closures' LLM references
        for name, (fn, desc) in agent._tools.items():
            pass  # tools capture agent._llm via closure set at __init__ time;
                  # we re-register them below to pick up the new mock

        # Re-register tools so closures capture the mocked LLM
        agent._tools.clear()
        gh2 = gh

        async def _fetch(owner: str, repo: str, pr_number: int) -> str:
            return await fetch_pr(owner, repo, pr_number, gh2)

        async def _analyze(filename: str) -> str:
            return await analyze_file(filename, gh2, llm_mock)

        async def _generate(
            owner: str, repo: str, pr_number: int,
            file_analyses: str, post_to_github: bool = False,
        ) -> str:
            return await generate_review(owner, repo, pr_number, file_analyses, gh2, llm_mock, post_to_github)

        agent.register_tool("fetch_pr", _fetch,
            "Fetch PR. Input: {owner, repo, pr_number}")
        agent.register_tool("analyze_file", _analyze,
            "Analyze file. Input: {filename}")
        agent.register_tool("generate_review", _generate,
            "Generate review. Input: {owner, repo, pr_number, file_analyses, post_to_github}")

        return agent, llm_mock

    @pytest.mark.asyncio
    async def test_full_react_loop(self):
        """Agent runs through fetch → analyze × 2 → generate → answer."""
        # LLM responses: 4 ReAct steps + 1 tool-impl call per analyze/generate
        # The ReAct loop calls llm.complete for each iteration; tool impls also call it.
        react_responses = list(AGENT_REACT_SEQUENCE)
        # Pad with the review text for generate_review's internal LLM call
        react_responses.insert(4, FAKE_REVIEW)  # called by generate_review tool impl
        # And two analyze_file LLM calls
        react_responses.insert(2, "ISSUE | CRITICAL | line 2 | SECURITY | SQL injection")
        react_responses.insert(3, "ISSUE | HIGH | line 3 | SECURITY | MD5 is weak")

        agent, _ = self._make_agent(react_responses)
        result = await agent.run('{"owner": "acme", "repo": "backend", "pr_number": 42}')

        assert result.answer  # agent produced an answer
        assert result.iterations <= 10

    @pytest.mark.asyncio
    async def test_json_input_parsed(self):
        """JSON input triggers the full direct-call pipeline and returns a review."""
        # Direct call path: analyze_file × 2 (one per FAKE_FILES) + generate_review × 1
        analysis_1 = "ISSUE | HIGH | line 2 | SECURITY | SQL injection"
        analysis_2 = "ISSUE | MEDIUM | line 3 | SECURITY | MD5 is weak"
        agent, llm_mock = self._make_agent([analysis_1, analysis_2, FAKE_REVIEW])
        result = await agent.run('{"owner": "acme", "repo": "backend", "pr_number": 42}')
        assert result.answer is not None
        assert result.iterations == 3

    @pytest.mark.asyncio
    async def test_plain_text_input(self):
        """Plain-text input (not JSON) is accepted."""
        agent, _ = self._make_agent([AGENT_FINAL_ANSWER])
        result = await agent.run("Review PR #42 in acme/backend")
        assert result.answer is not None

    @pytest.mark.asyncio
    async def test_review_kind_threaded_from_input_to_analyze_file(self):
        """The actual wiring this depends on: CodeReviewAgent.run() parses
        review_kind out of the input JSON and passes it to every analyze_file
        call — this is what _run_review's clarity call site in incident_loop.py
        relies on to get clarity-appropriate criteria applied."""
        gh = make_github_mock()
        gh.get_pr = AsyncMock(return_value=FAKE_PR)
        gh.get_pr_diff = AsyncMock(return_value=FAKE_FILES)
        agent = CodeReviewAgent(github=gh)
        llm_mock = MagicMock()
        llm_mock.complete = AsyncMock(
            side_effect=["LOOKS CORRECT", "LOOKS CORRECT", FAKE_REVIEW]
        )
        agent._llm = llm_mock

        await agent.run(
            '{"owner": "acme", "repo": "backend", "pr_number": 42, "review_kind": "clarity"}'
        )

        # Both analyze_file calls (one per FAKE_FILES entry) must have received
        # the clarity framing, not the default fix framing.
        analyze_calls = llm_mock.complete.call_args_list[:2]
        for call in analyze_calls:
            prompt_sent = call[1]["messages"][0]["content"]
            assert "OBSERVABILITY ADDITION" in prompt_sent
            assert "does the fix address the actual root cause" not in prompt_sent

    @pytest.mark.asyncio
    async def test_github_error_returns_error_answer(self):
        """A GitHub error on fetch_pr returns an error answer without calling LLM."""
        gh = make_github_mock()
        gh.get_pr = AsyncMock(side_effect=GitHubError(404, "Not Found"))

        agent = CodeReviewAgent(github=gh)
        llm_mock = MagicMock()
        llm_mock.complete = AsyncMock()
        agent._llm = llm_mock

        result = await agent.run('{"owner": "a", "repo": "b", "pr_number": 999}')

        assert "GitHub error" in result.answer
        assert "404" in result.answer
        assert result.iterations == 1
        llm_mock.complete.assert_not_called()


# ---------------------------------------------------------------------------
# Live integration test (opt-in, requires real credentials)
# ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.asyncio
async def test_live_review():
    """
    Real end-to-end test against GitHub.

    Set these env vars before running:
        GITHUB_TOKEN=ghp_...
        TEST_OWNER=your-org
        TEST_REPO=your-repo
        TEST_PR_NUMBER=42

    Run with:
        pytest tests/test_code_review_agent.py -m live -s
    """
    import os

    owner = os.environ.get("TEST_OWNER")
    repo = os.environ.get("TEST_REPO")
    pr_number = os.environ.get("TEST_PR_NUMBER")

    if not all([owner, repo, pr_number]):
        pytest.skip("TEST_OWNER / TEST_REPO / TEST_PR_NUMBER not set")

    agent = CodeReviewAgent()
    result = await agent.run(
        json.dumps({"owner": owner, "repo": repo, "pr_number": int(pr_number)})
    )

    print("\n" + "=" * 60)
    print(result.answer)
    print("=" * 60)
    print(f"Completed in {result.iterations} iteration(s)")

    assert result.answer
    assert "## Summary" in result.answer or "Code Review" in result.answer
