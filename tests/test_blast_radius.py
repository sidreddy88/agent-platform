"""
Tests for BlastRadiusGuard.

Covers:
  - Protected path detection (migrations, auth, secrets, lockfiles, CI, infra)
  - Max file count enforcement
  - Max lines added / deleted enforcement
  - Combinations of multiple violations
  - Incident pipeline blast radius escalation

Run:
    pytest tests/test_blast_radius.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.blast_radius import (
    DEFAULT_MAX_FILES,
    DEFAULT_MAX_LINES_ADDED,
    DEFAULT_MAX_LINES_DELETED,
    BlastRadiusGuard,
    BlastRadiusResult,
    blast_radius_guard,
)

# ---------------------------------------------------------------------------
# BlastRadiusResult
# ---------------------------------------------------------------------------

class TestBlastRadiusResult:
    def test_allowed_result_has_ok_reason(self):
        r = BlastRadiusResult(allowed=True)
        assert r.reason == "OK"
        assert r.violations == []

    def test_blocked_result_joins_violations(self):
        r = BlastRadiusResult(
            allowed=False,
            violations=["Too many files: 6 > limit 5", "Protected path 'auth/login.js'"],
        )
        assert "Too many files" in r.reason
        assert "Protected path" in r.reason

    def test_single_violation_reason(self):
        r = BlastRadiusResult(allowed=False, violations=["Too many files: 6 > limit 5"])
        assert r.reason == "Too many files: 6 > limit 5"


# ---------------------------------------------------------------------------
# Protected path detection
# ---------------------------------------------------------------------------

class TestProtectedPaths:
    def setup_method(self):
        self.guard = BlastRadiusGuard()

    def _assert_blocked(self, path: str):
        result = self.guard.check([path])
        assert not result.allowed, f"Expected {path!r} to be blocked"
        assert any("Protected path" in v for v in result.violations)

    def _assert_allowed(self, path: str):
        result = self.guard.check([path])
        assert not any("Protected path" in v for v in result.violations), \
            f"Expected {path!r} to be allowed, got: {result.violations}"

    # Migrations
    def test_blocks_migrations_dir(self):
        self._assert_blocked("migrations/0042_add_user_table.py")

    def test_blocks_nested_migrations(self):
        self._assert_blocked("app/db/migrations/001_init.sql")

    def test_blocks_migration_in_filename(self):
        self._assert_blocked("scripts/run_migration.sh")

    # Auth / security
    def test_blocks_auth_dir(self):
        self._assert_blocked("auth/middleware.js")

    def test_blocks_nested_auth(self):
        self._assert_blocked("app/routes/authentication/login.py")

    def test_blocks_authorization_dir(self):
        self._assert_blocked("services/authorization/policy.py")

    # Secrets and credentials
    def test_blocks_dotenv(self):
        self._assert_blocked(".env")

    def test_blocks_dotenv_production(self):
        self._assert_blocked(".env.production")

    def test_blocks_pem_file(self):
        self._assert_blocked("certs/server.pem")

    def test_blocks_key_file(self):
        self._assert_blocked("keys/private.key")

    def test_blocks_credentials_file(self):
        self._assert_blocked("config/credentials.json")

    # Lockfiles
    def test_blocks_package_lock(self):
        self._assert_blocked("package-lock.json")

    def test_blocks_yarn_lock(self):
        self._assert_blocked("yarn.lock")

    def test_blocks_poetry_lock(self):
        self._assert_blocked("poetry.lock")

    def test_blocks_go_sum(self):
        self._assert_blocked("go.sum")

    # CI/CD pipelines
    def test_blocks_github_workflow(self):
        self._assert_blocked(".github/workflows/ci.yml")

    def test_blocks_gitlab_ci(self):
        self._assert_blocked(".gitlab-ci.yml")

    def test_blocks_jenkinsfile(self):
        self._assert_blocked("Jenkinsfile")

    # Infrastructure
    def test_blocks_terraform_file(self):
        self._assert_blocked("infra/main.tf")

    def test_blocks_terraform_vars(self):
        self._assert_blocked("infra/prod.tfvars")

    def test_blocks_dockerfile(self):
        self._assert_blocked("Dockerfile")

    def test_blocks_docker_compose(self):
        self._assert_blocked("docker-compose.yml")

    # Safe paths — should NOT be blocked
    def test_allows_regular_service_file(self):
        self._assert_allowed("routes/services/image.js")

    def test_allows_test_file(self):
        self._assert_allowed("tests/services/image.test.js")

    def test_allows_readme(self):
        self._assert_allowed("README.md")

    def test_allows_src_file(self):
        self._assert_allowed("src/components/Button.tsx")

    def test_allows_util_file(self):
        self._assert_allowed("utils/formatting.py")


# ---------------------------------------------------------------------------
# Max file count
# ---------------------------------------------------------------------------

class TestMaxFiles:
    def test_at_limit_is_allowed(self):
        guard = BlastRadiusGuard(max_files=3)
        files = [f"src/file{i}.js" for i in range(3)]
        result = guard.check(files)
        assert not any("Too many files" in v for v in result.violations)

    def test_over_limit_is_blocked(self):
        guard = BlastRadiusGuard(max_files=3)
        files = [f"src/file{i}.js" for i in range(4)]
        result = guard.check(files)
        assert not result.allowed
        assert any("Too many files" in v for v in result.violations)
        assert "4 > limit 3" in result.reason

    def test_default_limit_is_five(self):
        assert DEFAULT_MAX_FILES == 5

    def test_single_file_always_allowed_by_count(self):
        guard = BlastRadiusGuard(max_files=1)
        result = guard.check(["src/file.js"])
        assert not any("Too many files" in v for v in result.violations)

    def test_empty_files_allowed(self):
        guard = BlastRadiusGuard()
        result = guard.check([])
        assert result.allowed


# ---------------------------------------------------------------------------
# Max lines added / deleted
# ---------------------------------------------------------------------------

class TestMaxLines:
    def test_at_addition_limit_is_allowed(self):
        guard = BlastRadiusGuard(max_lines_added=50)
        result = guard.check(["src/file.js"], additions=50)
        assert not any("additions" in v for v in result.violations)

    def test_over_addition_limit_is_blocked(self):
        guard = BlastRadiusGuard(max_lines_added=50)
        result = guard.check(["src/file.js"], additions=51)
        assert not result.allowed
        assert any("51 lines > limit 50" in v for v in result.violations)

    def test_at_deletion_limit_is_allowed(self):
        guard = BlastRadiusGuard(max_lines_deleted=30)
        result = guard.check(["src/file.js"], deletions=30)
        assert not any("deletions" in v for v in result.violations)

    def test_over_deletion_limit_is_blocked(self):
        guard = BlastRadiusGuard(max_lines_deleted=30)
        result = guard.check(["src/file.js"], deletions=31)
        assert not result.allowed
        assert any("31 lines > limit 30" in v for v in result.violations)

    def test_zero_lines_always_allowed(self):
        guard = BlastRadiusGuard()
        result = guard.check(["src/file.js"], additions=0, deletions=0)
        assert result.allowed

    def test_default_limits(self):
        assert DEFAULT_MAX_LINES_ADDED == 500
        assert DEFAULT_MAX_LINES_DELETED == 500


# ---------------------------------------------------------------------------
# Multiple violations accumulate
# ---------------------------------------------------------------------------

class TestMultipleViolations:
    def test_path_and_file_count_both_reported(self):
        guard = BlastRadiusGuard(max_files=1)
        files = ["auth/login.js", "src/a.js"]  # protected + too many
        result = guard.check(files)
        assert not result.allowed
        assert len(result.violations) >= 2
        assert any("Protected path" in v for v in result.violations)
        assert any("Too many files" in v for v in result.violations)

    def test_all_three_violation_types(self):
        guard = BlastRadiusGuard(max_files=1, max_lines_added=10, max_lines_deleted=5)
        result = guard.check(
            ["auth/login.js", "src/a.js"],  # protected + too many files
            additions=20,
            deletions=10,
        )
        assert not result.allowed
        assert len(result.violations) >= 3

    def test_multiple_protected_files_each_reported(self):
        guard = BlastRadiusGuard()
        result = guard.check(["migrations/001.sql", ".env", "yarn.lock"])
        protected_violations = [v for v in result.violations if "Protected path" in v]
        assert len(protected_violations) == 3  # one per protected file


# ---------------------------------------------------------------------------
# is_protected helper
# ---------------------------------------------------------------------------

class TestIsProtected:
    def test_protected_returns_true(self):
        guard = BlastRadiusGuard()
        assert guard.is_protected("migrations/001.sql") is True

    def test_safe_returns_false(self):
        guard = BlastRadiusGuard()
        assert guard.is_protected("routes/services/image.js") is False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_singleton_exists(self):
        assert blast_radius_guard is not None
        assert isinstance(blast_radius_guard, BlastRadiusGuard)

    def test_singleton_uses_defaults(self):
        assert blast_radius_guard.max_files == DEFAULT_MAX_FILES
        assert blast_radius_guard.max_lines_added == DEFAULT_MAX_LINES_ADDED


# ---------------------------------------------------------------------------
# Integration: FixGenerationAgent blast radius gate
# ---------------------------------------------------------------------------

class TestFixGenerationBlastRadius:
    @pytest.mark.asyncio
    async def test_protected_file_path_blocks_pr_creation(self):
        """If the target file is in a protected path, FixGen returns without creating a PR."""
        from app.agents.fix_generation import FixGenerationAgent
        from app.models.events import ErrorEvent, EventSource, IncidentState

        # Stack trace in description so Strategy 1 (_parse_stack_trace) resolves the target
        # without needing an LLM call or GitHub code search.
        event = ErrorEvent(
            source=EventSource.CLOUDWATCH,
            error_type="S3_NO_SUCH_KEY",
            title="test",
            description=(
                "NoSuchKey: The specified key does not exist.\n"
                "    at moveAndRemoveFileFromS3 (/app/routes/services/image.js:42:5)"
            ),
            service="image-service",
        )
        incident = IncidentState(error_event=event)
        incident.diagnosis = "NoSuchKey in S3"
        incident.confidence = 0.85

        _file_content = "function moveAndRemoveFileFromS3() {}"

        agent = FixGenerationAgent.__new__(FixGenerationAgent)
        agent._github = MagicMock()
        agent._llm = MagicMock()
        agent._rag = None
        agent._owner = "org"
        agent._repo = "repo"

        # Patch BlastRadiusGuard to always return a violation
        with patch("app.agents.fix_generation.BlastRadiusGuard") as MockGuard:
            mock_instance = MagicMock()
            mock_instance.check.return_value = MagicMock(
                allowed=False,
                violations=["Protected path 'routes/services/image.js' matches 'auth/**'"],
                reason="Protected path 'routes/services/image.js' matches 'auth/**'",
            )
            MockGuard.return_value = mock_instance

            agent._github.get_default_branch = AsyncMock(return_value="main")
            agent._github.get_file_contents = AsyncMock(return_value=(_file_content, "abc123"))
            _new_fn = "function moveAndRemoveFileFromS3() { try {} catch(e) {} }"
            agent._llm.complete_with_tools = AsyncMock(side_effect=[
                ("", [{"id": "c1", "name": "apply_edit", "input": {"new_text": _new_fn}}], "tool_use"),
                ("", [], "end_turn"),
            ])

            result, steps = await agent.fix_with_steps(incident)

        assert result.blast_radius_violation is True
        assert result.pr_url is None
        assert result.pr_number is None
        assert len(result.blast_radius_violations) > 0
        assert "BLAST_RADIUS_VIOLATION" in result.fix_description
        # No GitHub writes should have happened
        agent._github.create_branch = MagicMock()
        agent._github.create_branch.assert_not_called()

    @pytest.mark.asyncio
    async def test_safe_fix_passes_blast_radius(self):
        """A fix within limits proceeds to PR creation normally."""
        from app.agents.fix_generation import FixGenerationAgent
        from app.models.events import ErrorEvent, EventSource, IncidentState

        old_fn = "async function moveAndRemoveFileFromS3(key) {\n  await s3.copy(key);\n}"
        new_fn = "async function moveAndRemoveFileFromS3(key) {\n  try {\n    await s3.copy(key);\n  } catch(e) {\n    if (e.code === 'NoSuchKey') return;\n    throw e;\n  }\n}"

        event = ErrorEvent(
            source=EventSource.CLOUDWATCH,
            error_type="S3_NO_SUCH_KEY",
            title="test",
            description=(
                "NoSuchKey: The specified key does not exist.\n"
                "    at moveAndRemoveFileFromS3 (/app/routes/services/image.js:42:5)"
            ),
            service="image-service",
        )
        incident = IncidentState(error_event=event)
        incident.diagnosis = "NoSuchKey"
        incident.confidence = 0.85

        agent = FixGenerationAgent.__new__(FixGenerationAgent)
        agent._owner = "org"
        agent._repo = "repo"
        agent._rag = None

        agent._github = MagicMock()
        agent._github.get_default_branch = AsyncMock(return_value="main")
        # get_file_contents calls (in order):
        #   1. _resolve_target verification
        #   2. main file fetch
        agent._github.get_file_contents = AsyncMock(
            side_effect=[
                (old_fn, "sha123"),
                (old_fn, "sha123"),
            ]
        )
        agent._github.create_issue = AsyncMock(return_value=(10, "https://github.com/org/repo/issues/10"))
        agent._github.get_branch_sha = AsyncMock(return_value="deadbeef")
        agent._github.create_branch = AsyncMock()
        agent._github.update_file = AsyncMock(return_value="newsha123")
        agent._github.create_pull_request = AsyncMock(return_value=(11, "https://github.com/org/repo/pull/11"))

        agent._llm = MagicMock()
        agent._llm.complete_with_tools = AsyncMock(side_effect=[
            ("", [{"id": "c1", "name": "apply_edit", "input": {"new_text": new_fn}}], "tool_use"),
            ("", [], "end_turn"),
        ])

        with patch("app.agents.fix_generation.BlastRadiusGuard") as MockGuard, \
             patch("app.services.sandbox.SandboxService") as MockSandbox:
            mock_br = MagicMock()
            mock_br.check.return_value = MagicMock(allowed=True, violations=[], reason="OK")
            MockGuard.return_value = mock_br

            mock_sb = MagicMock()
            mock_sb.run = AsyncMock(return_value=MagicMock(passed=True, output="Tests passed", error=None))
            MockSandbox.return_value = mock_sb

            result, steps = await agent.fix_with_steps(incident)

        assert result.blast_radius_violation is False
        assert result.pr_url == "https://github.com/org/repo/pull/11"
        assert any("Blast radius OK" in s for s in steps)


# ---------------------------------------------------------------------------
# Integration: incident pipeline blast radius escalation
# ---------------------------------------------------------------------------

class TestIncidentLoopBlastRadius:
    @pytest.mark.asyncio
    async def test_blast_radius_violation_escalates_to_human(self):
        """When FixGen returns a blast radius violation, incident goes to AWAITING_APPROVAL."""
        from app.agents.fix_generation import FixResult
        from app.models.events import ErrorEvent, EventSource, IncidentStatus
        from app.services.incident_loop import IncidentLoop
        from app.services.incident_store import IncidentStore

        store = IncidentStore.__new__(IncidentStore)
        store._incidents = {}
        store._monitor_pr_map = {}

        from app.agents.diagnosis import DiagnosisResult
        from app.agents.triage import TriageResult

        loop = IncidentLoop.__new__(IncidentLoop)
        loop._running = False
        loop._rag = None
        loop._dedup_stats = {"sql_dedup": 0, "regression": 0, "rag_hit": 0, "cold_start": 0}
        loop._triage = MagicMock()
        loop._triage.triage = AsyncMock(return_value=TriageResult(
            decision="real", severity="P2", blast_radius="single_service",
            occurrences_24h=10, duplicate_pr=None, reasoning="real incident",
        ))
        loop._diagnosis = MagicMock()
        loop._diagnosis.diagnose = AsyncMock(return_value=DiagnosisResult(
            root_cause="NoSuchKey", confidence=0.85, escalate=False,
        ))
        loop._fix_agent = MagicMock()
        # _run_fix calls fix_with_steps; blast_radius violation has no pr_url
        loop._fix_agent.fix_with_steps = AsyncMock(return_value=(FixResult(
            issue_url=None,
            pr_url=None,
            pr_number=None,
            branch="fix/test-branch",
            fix_description="BLAST_RADIUS_VIOLATION: Protected path 'migrations/001.sql'",
            blast_radius_violation=True,
            blast_radius_violations=["Protected path 'migrations/001.sql' matches 'migrations/**'"],
        ), []))
        loop._review_agent = MagicMock()

        with (
            patch("app.services.incident_loop.incident_store", store),
            patch("app.services.incident_loop.alerting_service") as mock_alert,
            patch("app.services.incident_loop.approval_service") as mock_approval,
        ):
            mock_alert.send_alert = AsyncMock()
            mock_approval.request_approval = AsyncMock()

            event = ErrorEvent(
                source=EventSource.CLOUDWATCH,
                error_type="S3_NO_SUCH_KEY",
                title="test",
                description="test",
                service="image-service",
            )
            await loop._process(event)

        incident = list(store._incidents.values())[0]

        # Should escalate to human, not create a PR
        assert incident.status == IncidentStatus.AWAITING_APPROVAL
        assert incident.pr_url is None
        assert "BLAST_RADIUS_VIOLATION" in (incident.fix_attempted or "")

        # Code review and PR approval gate should NOT have been called
        loop._review_agent.run.assert_not_called()
        mock_approval.request_approval.assert_not_called()

        # Slack notification was sent
        mock_alert.send_alert.assert_called()
        call_titles = [
            call.args[0].title if call.args else call.kwargs.get("alert", MagicMock()).title
            for call in mock_alert.send_alert.call_args_list
        ]
        assert any("blast radius" in t.lower() for t in call_titles)
