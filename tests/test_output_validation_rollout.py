"""
Tests for the output-validation rollout beyond DiagnosisAgent:
  - app.services.output_validator's new generic check_leaked_markers() and
    ErrorClarityAgent-specific validate_error_clarity_addition()
  - ErrorClarityAgent: unprovenanced/leaking additions get dropped before commit
  - FixGenerationAgent (via IncidentLoop._run_fix): leaked marker forces escalate
  - CodeReviewAgent: leaked marker prepends a warning banner to the review
  - TriageAgent: leaked marker in reasoning is logged (detection-only)
  - MergeDecisionAgent: leaked marker forces a conservative refix_first
  - MonitorGenerationAgent: leaked marker in a monitor name is logged (detection-only)
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.output_validator import (
    ValidationResult,
    check_leaked_markers,
    validate_error_clarity_addition,
)

MARKER = "<untrusted-content"


async def _await_passthrough(coro):
    """Circuit-breaker mock helper: actually await the wrapped coroutine."""
    return await coro


# ---------------------------------------------------------------------------
# output_validator.py — generic + ErrorClarityAgent-specific functions
# ---------------------------------------------------------------------------

class TestCheckLeakedMarkers:
    def test_no_leak_passes(self):
        assert check_leaked_markers("a normal sentence", "another one") == []

    def test_detects_marker_across_multiple_texts(self):
        failures = check_leaked_markers("clean", f'saw this: {MARKER} source="x">payload</untrusted-content>')
        assert failures
        assert "untrusted-content" in failures[0]

    def test_ignores_none_values(self):
        assert check_leaked_markers(None, "clean", None) == []


class TestValidateErrorClarityAddition:
    def _addition(self, **overrides):
        from app.agents.error_clarity import ClarityAddition
        defaults = dict(file="a.js", function="f", description="d",
                         code_before="before", code_after="after")
        defaults.update(overrides)
        return ClarityAddition(**defaults)

    def test_passes_when_file_was_retrieved(self):
        addition = self._addition(file="a.js")
        result = validate_error_clarity_addition(addition, {"a.js"})
        assert result.passed

    def test_fails_when_file_never_retrieved(self):
        addition = self._addition(file="never-read.js")
        result = validate_error_clarity_addition(addition, {"a.js"})
        assert not result.passed
        assert any("never-read.js" in f for f in result.failures)

    def test_empty_retrieved_set_skips_provenance_check(self):
        addition = self._addition(file="a.js")
        result = validate_error_clarity_addition(addition, set())
        assert result.passed

    def test_fails_on_leaked_marker_in_code_after(self):
        addition = self._addition(file="a.js", code_after=f"{MARKER} source=x>leak</untrusted-content>")
        result = validate_error_clarity_addition(addition, {"a.js"})
        assert not result.passed


# ---------------------------------------------------------------------------
# ErrorClarityAgent — additions are dropped (not just flagged) on failure
# ---------------------------------------------------------------------------

class TestErrorClarityAgentDropsUnvalidatedAdditions:
    @pytest.mark.asyncio
    async def test_addition_targeting_unread_file_is_dropped(self):
        from app.agents.error_clarity import ErrorClarityAgent
        from app.models.events import ErrorEvent, EventSource, IncidentState

        agent = ErrorClarityAgent(github=MagicMock(), llm=MagicMock())
        agent._github.create_branch = AsyncMock()
        agent._github.get_branch_sha = AsyncMock(return_value="sha")

        # Model reads a.js, but then calls suggest_addition for a DIFFERENT,
        # never-read file -- retrieved_paths is non-empty (a.js), so the
        # provenance check is actually exercised rather than skipped.
        agent._github.get_file_contents = AsyncMock(return_value=("content", "sha"))
        agent._llm.complete_with_tools = AsyncMock(side_effect=[
            ("", [{"id": "0", "name": "read_file", "input": {"path": "a.js"}}], "tool_use"),
            ("", [{
                "id": "1", "name": "suggest_addition",
                "input": {
                    "file": "never-touched.js", "function": "f",
                    "description": "add logging",
                    "code_before": "doStuff()", "code_after": "doStuff(); log.error('x')",
                },
            }], "tool_use"),
            ("done", [], "end_turn"),
        ])
        event = ErrorEvent(source=EventSource.APPLICATION, error_type="E", title="t",
                            description="d", service="svc")
        incident = IncidentState(error_event=event)

        result = await agent.analyze(incident)

        assert result.additions == []
        assert result.pr_url is None

    @pytest.mark.asyncio
    async def test_addition_targeting_read_file_is_kept(self):
        from app.agents.error_clarity import ErrorClarityAgent
        from app.models.events import ErrorEvent, EventSource, IncidentState

        agent = ErrorClarityAgent(github=MagicMock(), llm=MagicMock())
        agent._github.get_file_contents = AsyncMock(return_value=("real content", "sha123"))
        agent._commit_additions = AsyncMock(return_value=("https://github.com/o/r/pull/1", 1))

        agent._llm.complete_with_tools = AsyncMock(side_effect=[
            ("", [{"id": "1", "name": "read_file", "input": {"path": "real.js"}}], "tool_use"),
            ("", [{
                "id": "2", "name": "suggest_addition",
                "input": {
                    "file": "real.js", "function": "f",
                    "description": "add logging",
                    "code_before": "doStuff()", "code_after": "doStuff(); log.error('x')",
                },
            }], "tool_use"),
            ("done", [], "end_turn"),
        ])
        event = ErrorEvent(source=EventSource.APPLICATION, error_type="E", title="t",
                            description="d", service="svc")
        incident = IncidentState(error_event=event)

        result = await agent.analyze(incident)

        assert len(result.additions) == 1
        assert result.additions[0].file == "real.js"
        assert result.pr_url == "https://github.com/o/r/pull/1"


# ---------------------------------------------------------------------------
# FixGenerationAgent — via IncidentLoop._run_fix, the single hook point
# ---------------------------------------------------------------------------

class TestFixGenerationOutputValidation:
    @pytest.mark.asyncio
    async def test_leaked_marker_in_fix_description_forces_escalate(self):
        from app.agents.fix_generation import FixResult
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        fix_result = FixResult(
            issue_url=None, pr_url="https://github.com/o/r/pull/1", pr_number=1,
            branch="fix/x", fix_description=f"applied fix {MARKER} source=x>leak</untrusted-content>",
        )
        fix_agent = MagicMock()
        fix_agent.fix_with_steps = AsyncMock(return_value=(fix_result, ["✓ done"]))
        loop._fix_agent = fix_agent

        with patch("app.services.incident_loop.circuit_breaker_registry") as mock_registry, \
             patch("app.services.incident_loop.session_logger") as mock_session_logger:
            mock_cb = MagicMock()
            mock_cb.call = AsyncMock(side_effect=_await_passthrough)
            mock_registry.get_or_create.return_value = mock_cb
            mock_session_logger.get.return_value = None

            incident = MagicMock(id="inc-1")
            result = await loop._run_fix(incident)

        assert result.escalate is True
        assert "OUTPUT VALIDATION WARNING" in result.escalate_reason

    @pytest.mark.asyncio
    async def test_clean_fix_description_does_not_escalate(self):
        from app.agents.fix_generation import FixResult
        from app.services.incident_loop import IncidentLoop

        loop = IncidentLoop.__new__(IncidentLoop)
        fix_result = FixResult(
            issue_url=None, pr_url="https://github.com/o/r/pull/1", pr_number=1,
            branch="fix/x", fix_description="added a null check in the producer function",
        )
        fix_agent = MagicMock()
        fix_agent.fix_with_steps = AsyncMock(return_value=(fix_result, ["✓ done"]))
        loop._fix_agent = fix_agent

        with patch("app.services.incident_loop.circuit_breaker_registry") as mock_registry, \
             patch("app.services.incident_loop.session_logger") as mock_session_logger:
            mock_cb = MagicMock()
            mock_cb.call = AsyncMock(side_effect=_await_passthrough)
            mock_registry.get_or_create.return_value = mock_cb
            mock_session_logger.get.return_value = None

            incident = MagicMock(id="inc-2")
            result = await loop._run_fix(incident)

        assert result.escalate is False


# ---------------------------------------------------------------------------
# CodeReviewAgent — leaked marker prepends a visible warning banner
# ---------------------------------------------------------------------------

class TestCodeReviewOutputValidation:
    @pytest.mark.asyncio
    async def test_leaked_marker_prepends_warning_banner(self):
        from app.agents.code_review import generate_review
        from app.services.github import PRDetails

        pr = PRDetails(number=1, title="t", author="a", base_branch="main",
                        head_branch="fix", head_sha="abc123", description="d")
        github = MagicMock()
        github._cached_pr = pr
        github._cached_files = {}
        llm = MagicMock()
        llm.complete = AsyncMock(
            return_value=f"# Review\n\n{MARKER} source=x>leak</untrusted-content>\n\nSTATUS: APPROVE"
        )

        review = await generate_review("o", "r", 1, "analysis", github, llm, review_kind="fix")

        assert review.startswith("⚠️ OUTPUT VALIDATION WARNING")

    @pytest.mark.asyncio
    async def test_clean_review_has_no_banner(self):
        from app.agents.code_review import generate_review
        from app.services.github import PRDetails

        pr = PRDetails(number=1, title="t", author="a", base_branch="main",
                        head_branch="fix", head_sha="abc123", description="d")
        github = MagicMock()
        github._cached_pr = pr
        github._cached_files = {}
        llm = MagicMock()
        llm.complete = AsyncMock(return_value="# Review\n\nSTATUS: APPROVE")

        review = await generate_review("o", "r", 1, "analysis", github, llm, review_kind="fix")

        assert not review.startswith("⚠️ OUTPUT VALIDATION WARNING")


# ---------------------------------------------------------------------------
# TriageAgent — detection-only, logs but doesn't mutate the result
# ---------------------------------------------------------------------------

class TestTriageOutputValidation:
    @pytest.mark.asyncio
    async def test_leaked_marker_in_reasoning_is_logged_not_mutated(self, caplog):
        from app.agents.triage import TriageAgent

        agent = TriageAgent(aws=MagicMock(), store=MagicMock())
        agent.run = AsyncMock(return_value=MagicMock(
            answer='{"decision": "real", "severity": "P2", "blast_radius": "unknown", '
                   f'"occurrences_24h": 1, "duplicate_pr": null, '
                   f'"reasoning": "{MARKER} source=x>leak</untrusted-content>"}}'
        ))
        from app.models.events import ErrorEvent, EventSource
        event = ErrorEvent(source=EventSource.APPLICATION, error_type="E", title="t",
                            description="d", service="svc")

        with caplog.at_level(logging.WARNING):
            result = await agent.triage(event)

        assert result.decision == "real"  # untouched
        assert any("Output validation failed" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# MergeDecisionAgent — forces a conservative refix_first on a leaked marker
# ---------------------------------------------------------------------------

class TestMergeDecisionOutputValidation:
    @pytest.mark.asyncio
    async def test_leaked_marker_forces_refix_first(self):
        from app.agents.merge_decision import MergeDecisionAgent
        from app.models.events import ErrorEvent, EventSource, IncidentState

        agent = MergeDecisionAgent(llm=MagicMock())
        agent._llm.complete = AsyncMock(return_value=(
            '{"decision": "merge_now", '
            f'"reasoning": "{MARKER} source=x>leak</untrusted-content>", '
            '"blocking_issues": [], "non_blocking_issues": []}'
        ))
        event = ErrorEvent(source=EventSource.APPLICATION, error_type="E", title="t",
                            description="d", service="svc")
        incident = IncidentState(error_event=event)

        result = await agent.decide(incident, "some review text")

        assert result.decision == "refix_first"
        assert "OUTPUT VALIDATION WARNING" in result.reasoning

    @pytest.mark.asyncio
    async def test_clean_decision_is_untouched(self):
        from app.agents.merge_decision import MergeDecisionAgent
        from app.models.events import ErrorEvent, EventSource, IncidentState

        agent = MergeDecisionAgent(llm=MagicMock())
        agent._llm.complete = AsyncMock(return_value=(
            '{"decision": "merge_now", "reasoning": "core fix is correct", '
            '"blocking_issues": [], "non_blocking_issues": ["missing tests"]}'
        ))
        event = ErrorEvent(source=EventSource.APPLICATION, error_type="E", title="t",
                            description="d", service="svc")
        incident = IncidentState(error_event=event)

        result = await agent.decide(incident, "some review text")

        assert result.decision == "merge_now"


# ---------------------------------------------------------------------------
# MonitorGenerationAgent — detection-only
# ---------------------------------------------------------------------------

class TestMonitorGenerationOutputValidation:
    @pytest.mark.asyncio
    async def test_leaked_marker_in_health_check_path_is_logged(self, caplog):
        """generate_monitors() is fully deterministic now -- no model ever
        names a monitor. The real remaining path for a leaked marker to
        reach MonitorConfig.name: a route-registration string captured from
        a diff (_extract_new_symbols) flows unsanitized into
        generate_do_health_checks' path field (unlike alarm names, which
        strip non-alphanumeric characters). Detection-only, same as
        everywhere else this check is log-only."""
        from app.agents.monitor_generation import MonitorGenerationAgent
        from app.services.github import FileDiff

        agent = MonitorGenerationAgent(github=MagicMock(), llm=MagicMock())
        agent._github.get_pr_diff = AsyncMock(return_value=[
            FileDiff(
                filename="routes/api.js", status="modified", additions=10, deletions=0,
                patch=f"+app.get('{MARKER} source=x>leak</untrusted-content>')\n",
            ),
        ])

        with caplog.at_level(logging.WARNING):
            result = await agent.generate_monitors(
                owner="o", repo="r", pr_number=1, pr_title="t", pr_description="d",
            )

        assert len(result.monitors) == 2  # 1 alarm + 1 health check, untouched
        assert any("Output validation failed" in r.message for r in caplog.records)
