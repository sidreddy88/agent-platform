"""
Tests for MonitorGenerationAgent.

Run:
    pytest tests/test_monitor_generation.py -v
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.monitor_generation import (
    MonitorConfig,
    MonitorGenerationAgent,
    MonitorGenerationResult,
    _parse_monitor_result,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(*, github=None, llm=None) -> MonitorGenerationAgent:
    """Build a MonitorGenerationAgent with mocked dependencies."""
    mock_github = github or MagicMock()
    mock_llm = llm or MagicMock()
    with patch("app.agents.monitor_generation.GitHubService", return_value=mock_github):
        with patch("app.agents.monitor_generation.LLMService", return_value=mock_llm):
            return MonitorGenerationAgent(github=mock_github, llm=mock_llm)


def _make_pr_details(title="fix: patch handler", head_branch="fix/patch", base_branch="main"):
    from app.services.github import PRDetails
    return PRDetails(
        number=42,
        title=title,
        description="Fixes S3 handler bug",
        author="dev",
        head_branch=head_branch,
        base_branch=base_branch,
        head_sha="abc123",
    )


def _make_file_diffs():
    from app.services.github import FileDiff
    return [
        FileDiff(
            filename="src/routes/image.js",
            status="modified",
            additions=80,
            deletions=5,
            patch="+async function handleImageUpload(req, res) {\n+  const key = req.params.id;\n",
        ),
        FileDiff(
            filename="src/utils/s3.js",
            status="modified",
            additions=30,
            deletions=2,
            patch="+function getSignedUrl(bucket, key) {\n",
        ),
    ]


# ---------------------------------------------------------------------------
# MonitorConfig dataclass
# ---------------------------------------------------------------------------

class TestMonitorConfig:
    def test_fields_present(self):
        m = MonitorConfig(
            monitor_type="cloudwatch_alarm",
            file="src/routes/image.js",
            name="auto-image-errors",
            config={"AlarmName": "auto-image-errors"},
        )
        assert m.monitor_type == "cloudwatch_alarm"
        assert m.file == "src/routes/image.js"
        assert m.created is False  # default

    def test_created_flag(self):
        m = MonitorConfig(
            monitor_type="cloudwatch_alarm",
            file="f",
            name="n",
            config={},
            created=True,
        )
        assert m.created is True


# ---------------------------------------------------------------------------
# MonitorGenerationResult dataclass
# ---------------------------------------------------------------------------

class TestMonitorGenerationResult:
    def test_defaults(self):
        r = MonitorGenerationResult(pr_number=1, repo="org/repo")
        assert r.monitors == []
        assert r.files_analyzed == 0
        assert r.monitors_created == 0
        assert r.coverage_ratio == 0.0
        assert r.dry_run is True


# ---------------------------------------------------------------------------
# _parse_monitor_result
# ---------------------------------------------------------------------------

class TestParseMonitorResult:
    def test_valid_json(self):
        answer = json.dumps({
            "monitors": [
                {
                    "monitor_type": "cloudwatch_alarm",
                    "file": "src/routes/image.js",
                    "name": "auto-image-errors",
                    "config": {"AlarmName": "auto-image-errors"},
                    "created": False,
                }
            ],
            "files_analyzed": 2,
            "monitors_created": 1,
            "coverage_ratio": 0.8,
        })
        result = _parse_monitor_result(answer, pr_number=42, repo="org/repo", dry_run=True)
        assert result.pr_number == 42
        assert result.repo == "org/repo"
        assert result.files_analyzed == 2
        assert result.monitors_created == 1
        assert result.coverage_ratio == 0.8
        assert len(result.monitors) == 1
        assert result.monitors[0].name == "auto-image-errors"

    def test_non_json_falls_back_to_empty(self):
        result = _parse_monitor_result(
            "I analyzed the PR. The coverage looks good.",
            pr_number=1, repo="org/repo", dry_run=False,
        )
        assert result.monitors == []
        assert result.files_analyzed == 0
        assert result.monitors_created == 0

    def test_empty_monitors_list(self):
        answer = json.dumps({
            "monitors": [],
            "files_analyzed": 1,
            "monitors_created": 0,
            "coverage_ratio": 0.0,
        })
        result = _parse_monitor_result(answer, pr_number=5, repo="org/repo", dry_run=True)
        assert result.monitors == []
        assert result.files_analyzed == 1

    def test_dry_run_flag_preserved(self):
        answer = json.dumps({"monitors": [], "files_analyzed": 0, "monitors_created": 0, "coverage_ratio": 0.0})
        r = _parse_monitor_result(answer, 1, "r", dry_run=False)
        assert r.dry_run is False

    def test_partial_json_in_longer_text(self):
        answer = 'Here is my analysis:\n' + json.dumps({
            "monitors": [],
            "files_analyzed": 2,
            "monitors_created": 0,
            "coverage_ratio": 0.0,
        }) + "\nDone."
        result = _parse_monitor_result(answer, 1, "r", dry_run=True)
        assert result.files_analyzed == 2


# ---------------------------------------------------------------------------
# generate_cloudwatch_alarms tool
# ---------------------------------------------------------------------------

class TestGenerateCloudwatchAlarms:
    @pytest.mark.asyncio
    async def test_one_alarm_for_75_lines(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_cloudwatch_alarms"]
        result_str = await fn(file="src/service.py", additions=75, new_functions=[], service_name="svc")
        alarms = json.loads(result_str)
        assert len(alarms) == 1

    @pytest.mark.asyncio
    async def test_one_alarm_for_74_lines(self):
        """Below 75 additions → still 1 alarm (max(1, ...))."""
        agent = _make_agent()
        fn, _ = agent._tools["generate_cloudwatch_alarms"]
        result_str = await fn(file="src/service.py", additions=74, new_functions=[], service_name="svc")
        alarms = json.loads(result_str)
        assert len(alarms) == 1

    @pytest.mark.asyncio
    async def test_two_alarms_for_150_lines(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_cloudwatch_alarms"]
        result_str = await fn(file="src/service.py", additions=150, new_functions=[], service_name="svc")
        alarms = json.loads(result_str)
        assert len(alarms) == 2

    @pytest.mark.asyncio
    async def test_js_file_uses_lambda_namespace(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_cloudwatch_alarms"]
        # Use a plain JS file with no route/api/handler/controller in the path
        result_str = await fn(file="src/utils/format.js", additions=75, new_functions=[], service_name="svc")
        alarms = json.loads(result_str)
        assert alarms[0]["Namespace"] == "AWS/Lambda"
        assert alarms[0]["MetricName"] == "Errors"

    @pytest.mark.asyncio
    async def test_route_file_uses_api_gateway_namespace(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_cloudwatch_alarms"]
        result_str = await fn(file="src/routes/user.ts", additions=75, new_functions=[], service_name="svc")
        alarms = json.loads(result_str)
        assert alarms[0]["Namespace"] == "AWS/ApiGateway"

    @pytest.mark.asyncio
    async def test_python_file_uses_ecs_namespace(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_cloudwatch_alarms"]
        result_str = await fn(file="app/models/user.py", additions=75, new_functions=[], service_name="svc")
        alarms = json.loads(result_str)
        assert alarms[0]["Namespace"] == "AWS/ECS"

    @pytest.mark.asyncio
    async def test_returns_valid_json(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_cloudwatch_alarms"]
        result_str = await fn(file="f.py", additions=75, new_functions=[], service_name="s")
        alarms = json.loads(result_str)  # must not raise
        assert isinstance(alarms, list)
        assert "AlarmName" in alarms[0]


# ---------------------------------------------------------------------------
# generate_do_health_checks tool
# ---------------------------------------------------------------------------

class TestGenerateDoHealthChecks:
    @pytest.mark.asyncio
    async def test_default_health_endpoint(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_do_health_checks"]
        result_str = await fn(file="src/api.py", additions=80, new_endpoints=[])
        checks = json.loads(result_str)
        assert len(checks) == 1
        assert checks[0]["path"] == "/health"

    @pytest.mark.asyncio
    async def test_custom_endpoints(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_do_health_checks"]
        result_str = await fn(file="src/api.py", additions=80, new_endpoints=["/users", "/orders"])
        checks = json.loads(result_str)
        assert len(checks) == 2
        paths = [c["path"] for c in checks]
        assert "/users" in paths and "/orders" in paths

    @pytest.mark.asyncio
    async def test_caps_at_3_checks_per_file(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_do_health_checks"]
        result_str = await fn(
            file="src/api.py", additions=80,
            new_endpoints=["/a", "/b", "/c", "/d", "/e"],
        )
        checks = json.loads(result_str)
        assert len(checks) == 3

    @pytest.mark.asyncio
    async def test_adds_leading_slash(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_do_health_checks"]
        result_str = await fn(file="src/api.py", additions=80, new_endpoints=["users"])
        checks = json.loads(result_str)
        assert checks[0]["path"] == "/users"

    @pytest.mark.asyncio
    async def test_returns_valid_json(self):
        agent = _make_agent()
        fn, _ = agent._tools["generate_do_health_checks"]
        result_str = await fn(file="f.py", additions=80, new_endpoints=[])
        checks = json.loads(result_str)  # must not raise
        assert isinstance(checks, list)
        assert "protocol" in checks[0]


# ---------------------------------------------------------------------------
# create_cloudwatch_alarm tool — dry-run
# ---------------------------------------------------------------------------

class TestCreateCloudwatchAlarmDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_returns_message(self):
        """Default dry_run=True — no boto3 call, returns DRY_RUN message."""
        agent = _make_agent()
        # Default: settings.create_monitors = False → dry_run = True
        fn, _ = agent._tools["create_cloudwatch_alarm"]
        result = await fn(alarm_config={"AlarmName": "test-alarm"})
        assert "DRY_RUN" in result
        assert "test-alarm" in result

    @pytest.mark.asyncio
    async def test_dry_run_does_not_call_boto3(self):
        agent = _make_agent()
        fn, _ = agent._tools["create_cloudwatch_alarm"]
        with patch("boto3.client") as mock_boto:
            await fn(alarm_config={"AlarmName": "test-alarm"})
            mock_boto.assert_not_called()


# ---------------------------------------------------------------------------
# analyze_pr_diff tool
# ---------------------------------------------------------------------------

class TestAnalyzePrDiff:
    @pytest.mark.asyncio
    async def test_returns_summary_string(self):
        mock_github = AsyncMock()
        mock_github.get_pr.return_value = _make_pr_details()
        mock_github.get_pr_diff.return_value = _make_file_diffs()

        agent = _make_agent(github=mock_github)
        fn, _ = agent._tools["analyze_pr_diff"]
        result = await fn(owner="org", repo="repo", pr_number=42)

        assert "PR #42" in result
        assert "src/routes/image.js" in result
        assert "+80" in result

    @pytest.mark.asyncio
    async def test_handles_github_error(self):
        from app.services.github import GitHubError
        mock_github = AsyncMock()
        mock_github.get_pr.side_effect = GitHubError(404, "Not Found")

        agent = _make_agent(github=mock_github)
        fn, _ = agent._tools["analyze_pr_diff"]
        result = await fn(owner="org", repo="repo", pr_number=99)

        assert "GitHub API error" in result

    @pytest.mark.asyncio
    async def test_empty_diff_returns_message(self):
        mock_github = AsyncMock()
        mock_github.get_pr.return_value = _make_pr_details()
        mock_github.get_pr_diff.return_value = []

        agent = _make_agent(github=mock_github)
        fn, _ = agent._tools["analyze_pr_diff"]
        result = await fn(owner="org", repo="repo", pr_number=42)

        assert "No changed files" in result

    @pytest.mark.asyncio
    async def test_no_token_returns_error(self):
        """When GitHub is None (no token), tool returns error string."""
        with patch("app.agents.monitor_generation.GitHubService", side_effect=ValueError("no token")):
            agent = MonitorGenerationAgent(github=None, llm=MagicMock())
        # _github is None
        fn, _ = agent._tools["analyze_pr_diff"]
        result = await fn(owner="org", repo="repo", pr_number=1)
        assert "Error" in result


# ---------------------------------------------------------------------------
# generate_monitors — integration (LLM mocked)
# ---------------------------------------------------------------------------

class TestGenerateMonitorsIntegration:
    @pytest.mark.asyncio
    async def test_success_returns_result(self):
        """Mock the LLM to return a JSON answer on the first iteration."""
        answer_json = json.dumps({
            "monitors": [
                {
                    "monitor_type": "cloudwatch_alarm",
                    "file": "src/routes/image.js",
                    "name": "auto-image-errors",
                    "config": {"AlarmName": "auto-image-errors"},
                    "created": False,
                }
            ],
            "files_analyzed": 2,
            "monitors_created": 1,
            "coverage_ratio": 0.93,
        })

        mock_llm = MagicMock()
        # The ReAct loop calls llm.complete() — return "Answer: <json>"
        mock_llm.complete = AsyncMock(return_value=f"Thought: done\nAnswer: {answer_json}")
        mock_llm.last_input_tokens = 0

        mock_github = AsyncMock()
        mock_github.get_pr.return_value = _make_pr_details()
        mock_github.get_pr_diff.return_value = _make_file_diffs()

        agent = _make_agent(github=mock_github, llm=mock_llm)

        result = await agent.generate_monitors(
            owner="org",
            repo="repo",
            pr_number=42,
            pr_title="fix: patch S3 handler",
        )

        assert isinstance(result, MonitorGenerationResult)
        assert result.pr_number == 42
        assert result.repo == "repo"
        assert result.monitors_created == 1
        assert result.coverage_ratio == 0.93
        assert len(result.monitors) == 1

    @pytest.mark.asyncio
    async def test_non_json_answer_returns_empty_result(self):
        """Non-JSON answer from LLM → empty result, not an exception."""
        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock(return_value="Thought: done\nAnswer: I analyzed the PR.")
        mock_llm.last_input_tokens = 0

        agent = _make_agent(llm=mock_llm)
        result = await agent.generate_monitors("org", "repo", 1)

        assert isinstance(result, MonitorGenerationResult)
        assert result.monitors == []
        assert result.files_analyzed == 0

    @pytest.mark.asyncio
    async def test_dry_run_default(self):
        """Default settings.create_monitors = False → dry_run = True."""
        mock_llm = MagicMock()
        mock_llm.complete = AsyncMock(
            return_value='Thought: x\nAnswer: {"monitors":[],"files_analyzed":0,"monitors_created":0,"coverage_ratio":0.0}'
        )
        mock_llm.last_input_tokens = 0

        agent = _make_agent(llm=mock_llm)
        result = await agent.generate_monitors("org", "repo", 1)

        assert result.dry_run is True

    def test_coverage_ratio_calculation(self):
        """Coverage ratio: monitors_generated / (total_lines / 75)."""
        # 75 lines changed, 1 monitor → ratio = 1.0
        answer = json.dumps({
            "monitors": [{"monitor_type": "cloudwatch_alarm", "file": "f", "name": "n", "config": {}, "created": False}],
            "files_analyzed": 1,
            "monitors_created": 1,
            "coverage_ratio": 1.0,
        })
        r = _parse_monitor_result(answer, 1, "r", dry_run=True)
        assert r.coverage_ratio == 1.0

        # 150 lines, 1 monitor → ratio = 0.5
        answer2 = json.dumps({
            "monitors": [{"monitor_type": "cloudwatch_alarm", "file": "f", "name": "n", "config": {}, "created": False}],
            "files_analyzed": 1,
            "monitors_created": 1,
            "coverage_ratio": 0.5,
        })
        r2 = _parse_monitor_result(answer2, 1, "r", dry_run=True)
        assert r2.coverage_ratio == 0.5


# ---------------------------------------------------------------------------
# Singleton / module-level wiring
# ---------------------------------------------------------------------------

class TestModuleWiring:
    def test_monitor_config_is_dataclass(self):
        from dataclasses import fields
        field_names = {f.name for f in fields(MonitorConfig)}
        assert {"monitor_type", "file", "name", "config", "created"} <= field_names

    def test_result_is_dataclass(self):
        from dataclasses import fields
        field_names = {f.name for f in fields(MonitorGenerationResult)}
        assert {"pr_number", "repo", "monitors", "files_analyzed", "monitors_created",
                "coverage_ratio", "dry_run"} <= field_names
