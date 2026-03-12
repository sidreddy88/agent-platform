"""
Tests for CICDAgent.

Two modes:
  - Unit tests (default): all GitHub + LLM calls are mocked — no network, no API keys needed.
  - Live test (opt-in):   uses real GitHub API against a public repo with known failures.

Run unit tests:
    pytest tests/test_cicd_agent.py -v

Run live test:
    pytest tests/test_cicd_agent.py -m live -s

The live test targets `nickjj/docker-flask-example` — a public repo that has
GitHub Actions configured and a history of real workflow runs (including failures).
You can override it with env vars:
    LIVE_OWNER=your-org LIVE_REPO=your-repo pytest tests/test_cicd_agent.py -m live -s
"""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.cicd import (
    CICDAgent,
    _classify,
    _extract_error_snippet,
    analyze_failure,
    get_run_logs,
    get_workflow_runs,
    search_codebase,
    suggest_fix,
)
from app.services.github import GitHubError, GitHubService

# ---------------------------------------------------------------------------
# Public repo used for live tests
# ---------------------------------------------------------------------------
LIVE_OWNER = os.environ.get("LIVE_OWNER", "actions")
LIVE_REPO = os.environ.get("LIVE_REPO", "runner")

# ---------------------------------------------------------------------------
# Sample log fixtures covering every failure type
# ---------------------------------------------------------------------------

LOGS = {
    "test_failure": """\
Run pytest tests/
FAILED tests/test_auth.py::test_login - AssertionError: assert 401 == 200
FAILED tests/test_auth.py::test_logout - AssertionError: expected True received False
2 failed, 18 passed in 3.42s
""",
    "build_error": """\
> tsc --noEmit
src/api/client.ts:42:18 - error TS2345: Argument of type 'string | undefined'
is not assignable to parameter of type 'string'.
src/api/client.ts:42:18
Found 1 error in 1 file.
""",
    "dependency": """\
npm install
npm ERR! Could not resolve dependency:
npm ERR! peer react@"^17.0.0" from react-dom@17.0.2
npm ERR! node_modules/react-dom
npm ERR!   react-dom@"17.0.2" from the root project
""",
    "timeout": """\
Run ./scripts/integration_test.sh
...running...
Error: The operation was canceled.
##[error]The job running on runner Hosted Agent has exceeded the maximum time limit of 360 minutes.
""",
    "flaky_test": """\
FAILED tests/test_api.py::test_fetch_users - AssertionError: assert [] != []
ECONNRESET: connection reset by peer
retry attempt 2 of 3
FAILED tests/test_api.py::test_fetch_users
""",
    "unknown": """\
Some unexpected output
Process completed with exit code 1
""",
    "long_log": "\n".join(
        [f"line {i}: build step output" for i in range(1, 400)]
        + ["ERROR: test_main.py::test_foo - AssertionError: assert 1 == 2"]
        + [f"line {i}: cleanup" for i in range(400, 430)]
    ),
}

FAKE_RUNS = [
    {
        "id": 111,
        "name": "CI",
        "status": "completed",
        "conclusion": "failure",
        "branch": "main",
        "commit_sha": "abc12345",
        "commit_message": "fix: update auth logic",
        "created_at": "2024-01-15T10:00:00Z",
        "html_url": "https://github.com/acme/backend/actions/runs/111",
    },
    {
        "id": 110,
        "name": "CI",
        "status": "completed",
        "conclusion": "success",
        "branch": "main",
        "commit_sha": "def67890",
        "commit_message": "chore: update deps",
        "created_at": "2024-01-14T09:00:00Z",
        "html_url": "https://github.com/acme/backend/actions/runs/110",
    },
]

FAKE_ANALYSIS = """\
FAILURE_TYPE: TEST_FAILURE

ROOT_CAUSE:
test_login asserts a 200 response but received 401. The auth middleware
is rejecting the test credentials.

KEY_ERROR:
AssertionError: assert 401 == 200

AFFECTED_FILES:
tests/test_auth.py, app/auth.py

SEARCH_QUERY:
login authentication middleware response status
"""

FAKE_FIX = """\
# CI Fix: TEST_FAILURE

## What Went Wrong
The login test expects HTTP 200 but the auth middleware returned 401.

## Recommended Fix

### Immediate Action
Check that test fixtures set up valid credentials before calling the login endpoint.

### Code Change (if applicable)
```python
# Before
response = client.post("/login", json={"user": "test"})
# After
response = client.post("/login", json={"user": "test", "password": "secret"})
```

### Verification
Re-run pytest tests/test_auth.py — both tests should pass.

## Prevention
Ensure test fixtures are kept in sync with auth schema changes.

## Confidence
HIGH — the error message clearly shows missing credentials.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_github_mock() -> MagicMock:
    gh = MagicMock(spec=GitHubService)
    gh.get_workflow_runs = AsyncMock(return_value=FAKE_RUNS)
    gh.get_run_logs = AsyncMock(return_value=LOGS["test_failure"])
    gh._cached_runs = None
    gh._cached_logs = {}
    gh._cached_analysis = {}
    return gh


def make_llm_mock(response: str = "Mocked LLM response") -> MagicMock:
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=response)
    return llm


# ---------------------------------------------------------------------------
# Unit tests — failure classifier
# ---------------------------------------------------------------------------

class TestClassify:
    @pytest.mark.parametrize("logs,expected", [
        (LOGS["test_failure"], "TEST_FAILURE"),
        (LOGS["build_error"], "BUILD_ERROR"),
        (LOGS["dependency"], "DEPENDENCY"),
        (LOGS["timeout"], "TIMEOUT"),
        (LOGS["flaky_test"], "FLAKY_TEST"),
        (LOGS["unknown"], "UNKNOWN"),
    ])
    def test_classifies_correctly(self, logs, expected):
        assert _classify(logs) == expected

    def test_timeout_takes_priority_over_test_failure(self):
        mixed = LOGS["timeout"] + "\nFAILED tests/test_foo.py::test_bar"
        assert _classify(mixed) == "TIMEOUT"

    def test_dependency_takes_priority_over_build_error(self):
        mixed = LOGS["dependency"] + "\nerror TS2345: type error"
        assert _classify(mixed) == "DEPENDENCY"

    def test_flaky_upgrades_test_failure(self):
        assert _classify(LOGS["flaky_test"]) == "FLAKY_TEST"

    def test_empty_logs_returns_unknown(self):
        assert _classify("") == "UNKNOWN"


# ---------------------------------------------------------------------------
# Unit tests — error snippet extraction
# ---------------------------------------------------------------------------

class TestExtractErrorSnippet:
    def test_extracts_error_from_long_log(self):
        snippet = _extract_error_snippet(LOGS["long_log"])
        assert "AssertionError" in snippet

    def test_short_log_returns_content(self):
        snippet = _extract_error_snippet(LOGS["test_failure"])
        assert "FAILED" in snippet

    def test_empty_log_returns_empty(self):
        assert _extract_error_snippet("") == ""


# ---------------------------------------------------------------------------
# Unit tests — get_workflow_runs tool
# ---------------------------------------------------------------------------

class TestGetWorkflowRuns:
    @pytest.mark.asyncio
    async def test_returns_formatted_run_list(self):
        gh = make_github_mock()
        result = await get_workflow_runs("acme", "backend", gh)

        assert "111" in result
        assert "failure" in result
        assert "success" in result
        assert "fix: update auth logic" in result

    @pytest.mark.asyncio
    async def test_caches_runs(self):
        gh = make_github_mock()
        await get_workflow_runs("acme", "backend", gh)
        assert gh._cached_runs == FAKE_RUNS

    @pytest.mark.asyncio
    async def test_handles_github_error(self):
        gh = make_github_mock()
        gh.get_workflow_runs = AsyncMock(side_effect=GitHubError(404, "Not Found"))
        result = await get_workflow_runs("acme", "bad-repo", gh)
        assert "GitHub error" in result

    @pytest.mark.asyncio
    async def test_empty_runs(self):
        gh = make_github_mock()
        gh.get_workflow_runs = AsyncMock(return_value=[])
        result = await get_workflow_runs("acme", "backend", gh)
        assert "No workflow runs found" in result


# ---------------------------------------------------------------------------
# Unit tests — get_run_logs tool
# ---------------------------------------------------------------------------

class TestGetRunLogs:
    @pytest.mark.asyncio
    async def test_returns_logs(self):
        gh = make_github_mock()
        result = await get_run_logs("acme", "backend", 111, gh)
        assert "AssertionError" in result

    @pytest.mark.asyncio
    async def test_caches_logs(self):
        gh = make_github_mock()
        await get_run_logs("acme", "backend", 111, gh)
        assert 111 in gh._cached_logs

    @pytest.mark.asyncio
    async def test_truncates_long_logs(self):
        gh = make_github_mock()
        gh.get_run_logs = AsyncMock(return_value="\n".join(f"line {i}" for i in range(500)))
        result = await get_run_logs("acme", "backend", 111, gh)
        assert "lines omitted" in result

    @pytest.mark.asyncio
    async def test_handles_github_error(self):
        gh = make_github_mock()
        gh.get_run_logs = AsyncMock(side_effect=GitHubError(403, "Forbidden"))
        result = await get_run_logs("acme", "backend", 111, gh)
        assert "GitHub error" in result


# ---------------------------------------------------------------------------
# Unit tests — analyze_failure tool
# ---------------------------------------------------------------------------

class TestAnalyzeFailure:
    @pytest.mark.asyncio
    async def test_analyzes_cached_logs(self):
        gh = make_github_mock()
        gh._cached_logs = {111: LOGS["test_failure"]}
        llm = make_llm_mock(FAKE_ANALYSIS)

        result = await analyze_failure(111, gh, llm)

        assert result == FAKE_ANALYSIS
        llm.complete.assert_called_once()
        prompt = llm.complete.call_args[1]["messages"][0]["content"]
        assert "TEST_FAILURE" in prompt

    @pytest.mark.asyncio
    async def test_caches_analysis(self):
        gh = make_github_mock()
        gh._cached_logs = {111: LOGS["test_failure"]}
        llm = make_llm_mock(FAKE_ANALYSIS)

        await analyze_failure(111, gh, llm)
        assert gh._cached_analysis["run_id"] == 111
        assert gh._cached_analysis["failure_type"] == "TEST_FAILURE"

    @pytest.mark.asyncio
    async def test_no_cached_logs_returns_error(self):
        gh = make_github_mock()
        gh._cached_logs = {}
        llm = make_llm_mock()

        result = await analyze_failure(999, gh, llm)
        assert "get_run_logs first" in result
        llm.complete.assert_not_called()

    @pytest.mark.parametrize("log_key,expected_type", [
        ("build_error", "BUILD_ERROR"),
        ("dependency", "DEPENDENCY"),
        ("timeout", "TIMEOUT"),
    ])
    @pytest.mark.asyncio
    async def test_passes_correct_failure_type_to_llm(self, log_key, expected_type):
        gh = make_github_mock()
        gh._cached_logs = {111: LOGS[log_key]}
        llm = make_llm_mock("analysis result")

        await analyze_failure(111, gh, llm)
        prompt = llm.complete.call_args[1]["messages"][0]["content"]
        assert expected_type in prompt


# ---------------------------------------------------------------------------
# Unit tests — search_codebase tool
# ---------------------------------------------------------------------------

class TestSearchCodebase:
    @pytest.mark.asyncio
    async def test_returns_rag_results(self):
        rag = MagicMock()
        from app.services.rag import CodeChunk
        rag.search = AsyncMock(return_value=[
            CodeChunk(
                chunk_id="abc",
                file_path="app/auth.py",
                language="python",
                start_line=1,
                end_line=20,
                content="def login(user, pw): ...",
                score=0.92,
            )
        ])
        result = await search_codebase("login authentication", rag)
        assert "app/auth.py" in result
        assert "0.92" in result

    @pytest.mark.asyncio
    async def test_no_rag_returns_unavailable(self):
        result = await search_codebase("query", None)
        assert "not configured" in result

    @pytest.mark.asyncio
    async def test_empty_results(self):
        rag = MagicMock()
        rag.search = AsyncMock(return_value=[])
        result = await search_codebase("obscure query", rag)
        assert "No relevant code found" in result


# ---------------------------------------------------------------------------
# Unit tests — suggest_fix tool
# ---------------------------------------------------------------------------

class TestSuggestFix:
    @pytest.mark.asyncio
    async def test_returns_fix_recommendation(self):
        gh = make_github_mock()
        gh._cached_analysis = {
            "run_id": 111,
            "failure_type": "TEST_FAILURE",
            "text": FAKE_ANALYSIS,
        }
        llm = make_llm_mock(FAKE_FIX)

        result = await suggest_fix(111, gh, llm, codebase_context="def login(): ...")
        assert result == FAKE_FIX
        prompt = llm.complete.call_args[1]["messages"][0]["content"]
        assert "TEST_FAILURE" in prompt
        assert "def login():" in prompt

    @pytest.mark.asyncio
    async def test_no_analysis_returns_error(self):
        gh = make_github_mock()
        gh._cached_analysis = {}
        llm = make_llm_mock()

        result = await suggest_fix(111, gh, llm)
        assert "analyze_failure first" in result
        llm.complete.assert_not_called()

    @pytest.mark.asyncio
    async def test_wrong_run_id_returns_error(self):
        gh = make_github_mock()
        gh._cached_analysis = {"run_id": 111, "failure_type": "TEST_FAILURE", "text": "..."}
        llm = make_llm_mock()

        result = await suggest_fix(999, gh, llm)  # different run_id
        assert "analyze_failure first" in result


# ---------------------------------------------------------------------------
# Unit tests — CICDAgent full loop
# ---------------------------------------------------------------------------

REACT_SEQUENCE = [
    # Step 1: list runs
    (
        "Thought: List recent workflow runs first.\n"
        'Action: get_workflow_runs\n'
        'Action Input: {"owner": "acme", "repo": "backend"}'
    ),
    # Step 2: fetch logs for the failed run
    (
        "Thought: Run 111 failed. Fetch logs.\n"
        'Action: get_run_logs\n'
        'Action Input: {"owner": "acme", "repo": "backend", "run_id": 111}'
    ),
    # Step 3: analyze
    (
        "Thought: Analyze the failure.\n"
        'Action: analyze_failure\n'
        'Action Input: {"run_id": 111}'
    ),
    # Step 4: search codebase
    (
        "Thought: Search for relevant code.\n"
        'Action: search_codebase\n'
        'Action Input: {"query": "login authentication middleware"}'
    ),
    # Step 5: suggest fix
    (
        "Thought: Generate the fix.\n"
        'Action: suggest_fix\n'
        'Action Input: {"run_id": 111, "codebase_context": "def login(): ..."}'
    ),
    # Step 6: final answer
    f"Thought: Done.\nAnswer: {FAKE_FIX}",
]


class TestCICDAgent:
    def _make_agent(self, llm_responses: list[str]) -> tuple[CICDAgent, MagicMock, MagicMock]:
        gh = make_github_mock()
        gh._cached_logs = {111: LOGS["test_failure"]}
        gh._cached_analysis = {
            "run_id": 111,
            "failure_type": "TEST_FAILURE",
            "text": FAKE_ANALYSIS,
        }

        llm_mock = MagicMock()
        llm_mock.complete = AsyncMock(side_effect=llm_responses)

        rag_mock = MagicMock()
        from app.services.rag import CodeChunk
        rag_mock.search = AsyncMock(return_value=[
            CodeChunk("x", "app/auth.py", "python", 1, 10, "def login(): ...", 0.9)
        ])

        agent = CICDAgent(github=gh, rag=rag_mock)

        # Re-register tools with mocked LLM
        agent._tools.clear()

        async def _runs(owner: str, repo: str, limit: int = 10) -> str:
            return await get_workflow_runs(owner, repo, gh, limit=limit)

        async def _logs(owner: str, repo: str, run_id: int) -> str:
            return await get_run_logs(owner, repo, run_id, gh)

        async def _analyze(run_id: int) -> str:
            return await analyze_failure(run_id, gh, llm_mock)

        async def _search(query: str) -> str:
            return await search_codebase(query, rag_mock)

        async def _fix(run_id: int, codebase_context: str = "") -> str:
            return await suggest_fix(run_id, gh, llm_mock, codebase_context)

        agent.register_tool("get_workflow_runs", _runs, "List runs. Input: {owner, repo, limit}")
        agent.register_tool("get_run_logs", _logs, "Get logs. Input: {owner, repo, run_id}")
        agent.register_tool("analyze_failure", _analyze, "Analyze. Input: {run_id}")
        agent.register_tool("search_codebase", _search, "Search. Input: {query}")
        agent.register_tool("suggest_fix", _fix, "Fix. Input: {run_id, codebase_context}")

        agent._llm = llm_mock
        return agent, llm_mock, gh

    @pytest.mark.asyncio
    async def test_json_input_reaches_super_run(self):
        agent, llm_mock, _ = self._make_agent([f"Thought: Done.\nAnswer: {FAKE_FIX}"])
        result = await agent.run('{"owner": "acme", "repo": "backend"}')
        assert result.answer is not None

    @pytest.mark.asyncio
    async def test_plain_text_input_accepted(self):
        agent, _, _ = self._make_agent([f"Thought: Done.\nAnswer: {FAKE_FIX}"])
        result = await agent.run("Check CI failures for acme/backend")
        assert result.answer is not None

    @pytest.mark.asyncio
    async def test_run_id_in_input_included_in_prompt(self):
        agent, llm_mock, _ = self._make_agent([f"Thought: Done.\nAnswer: done"])
        await agent.run('{"owner": "acme", "repo": "backend", "run_id": 12345}')
        prompt_sent = llm_mock.complete.call_args[1]["messages"][0]["content"]
        assert "12345" in prompt_sent

    @pytest.mark.asyncio
    async def test_max_iterations_not_exceeded(self):
        no_answer = (
            "Thought: still looking\n"
            'Action: get_workflow_runs\n'
            'Action Input: {"owner": "a", "repo": "b"}'
        )
        agent, _, _ = self._make_agent([no_answer] * 20)
        result = await agent.run('{"owner": "a", "repo": "b"}')
        assert "unable to find" in result.answer.lower()
        assert result.iterations == 10

    @pytest.mark.asyncio
    async def test_works_without_rag(self):
        gh = make_github_mock()
        llm_mock = MagicMock()
        llm_mock.complete = AsyncMock(return_value=f"Thought: Done.\nAnswer: {FAKE_FIX}")

        agent = CICDAgent(github=gh, rag=None)  # no RAG
        agent._llm = llm_mock
        result = await agent.run('{"owner": "acme", "repo": "backend"}')
        assert result.answer is not None


# ---------------------------------------------------------------------------
# Live integration test — real GitHub Actions runs
# ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.asyncio
async def test_live_failed_run_analysis():
    """
    Hits the real GitHub API to find a failed workflow run and diagnoses it.

    Uses the `actions/runner` public repo which has a long history of CI runs
    including failures. Override with env vars:
        LIVE_OWNER=your-org LIVE_REPO=your-repo

    Requires GITHUB_TOKEN in .env.

    Run with:
        pytest tests/test_cicd_agent.py -m live -s
    """
    from app.services.github import GitHubService

    print(f"\nTarget repo: {LIVE_OWNER}/{LIVE_REPO}")

    gh = GitHubService()

    # Step 1: get recent runs
    print("Fetching workflow runs...")
    runs = await gh.get_workflow_runs(LIVE_OWNER, LIVE_REPO, limit=20)
    assert runs, "No workflow runs found"

    print(f"Found {len(runs)} runs:")
    for r in runs[:5]:
        print(f"  [{r['id']}] {r['name']} — {r['conclusion'] or r['status']}  branch={r['branch']}")

    # Step 2: find a failed run
    failed = next((r for r in runs if r["conclusion"] == "failure"), None)
    if not failed:
        pytest.skip(f"No failed runs found in the last {len(runs)} runs for {LIVE_OWNER}/{LIVE_REPO}")

    print(f"\nAnalyzing failed run #{failed['id']}: \"{failed['commit_message']}\"")

    # Step 3: fetch jobs to see which failed
    jobs = await gh.get_run_jobs(LIVE_OWNER, LIVE_REPO, failed["id"])
    failed_jobs = [j for j in jobs if j["conclusion"] in ("failure", "timed_out")]
    print(f"Failed jobs: {[j['name'] for j in failed_jobs]}")

    # Step 4: fetch logs
    print("Fetching logs...")
    logs = await gh.get_run_logs(LIVE_OWNER, LIVE_REPO, failed["id"])
    assert logs
    print(f"Log size: {len(logs)} chars")

    # Step 5: classify failure
    from app.agents.cicd import _classify, _extract_error_snippet
    failure_type = _classify(logs)
    snippet = _extract_error_snippet(logs)

    print(f"\nFailure type: {failure_type}")
    print(f"\nError snippet:\n{snippet[:500]}")

    assert failure_type in ("TEST_FAILURE", "BUILD_ERROR", "DEPENDENCY", "TIMEOUT", "FLAKY_TEST", "UNKNOWN")

    # Step 6: run the full agent
    print("\nRunning CICDAgent...")
    agent = CICDAgent(github=gh)
    result = await agent.run(
        f'{{"owner": "{LIVE_OWNER}", "repo": "{LIVE_REPO}", "run_id": {failed["id"]}}}'
    )

    print("\n" + "=" * 60)
    print(result.answer)
    print("=" * 60)
    print(f"Completed in {result.iterations} iteration(s)")

    assert result.answer
    assert result.iterations <= 10
