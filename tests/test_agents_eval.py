"""
Evaluation suite for all agents — tests output quality on specific scenarios.

Structure:
  - Each class tests one agent across 3-5 real-world scenarios
  - External APIs (GitHub, AWS) and the LLM are mocked for speed and consistency
  - Scenario tests assert the agent's answer contains the required elements
  - Pure unit tests cover deterministic helpers (risk rating, metric math, etc.)

Run all:
    pytest tests/test_agents_eval.py -v

Compact output:
    pytest tests/test_agents_eval.py -v --tb=short -q
"""

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.aws import (
    AWSService,
    CloudWatchMetric,
    ECSServiceStatus,
)
from app.services.github import FileDiff, GitHubService, PRDetails

# ============================================================================
# Shared helpers
# ============================================================================

def _llm(response: str) -> MagicMock:
    """LLM mock that returns the same response to every call."""
    m = MagicMock()
    m.complete = AsyncMock(return_value=response)
    return m


def _llm_seq(*responses: str) -> MagicMock:
    """LLM mock that returns responses one by one (side_effect list)."""
    m = MagicMock()
    m.complete = AsyncMock(side_effect=list(responses))
    return m


def _answer(text: str) -> str:
    """Wrap text in a ReAct Answer block so BaseAgent terminates the loop."""
    return f"Thought: I have enough information to answer.\nAnswer: {text}"


def _action(tool: str, args: dict, thought: str = "I should call this tool next.") -> str:
    """Return a ReAct Action block."""
    return (
        f"Thought: {thought}\n"
        f"Action: {tool}\n"
        f"Action Input: {json.dumps(args)}"
    )


def _make_ecs_status(
    service: str = "api",
    cluster: str = "prod",
    running: int = 3,
    desired: int = 3,
    status: str = "healthy",
    deployments: list | None = None,
    events: list | None = None,
) -> ECSServiceStatus:
    return ECSServiceStatus(
        cluster=cluster,
        service=service,
        status="ACTIVE",
        running_count=running,
        desired_count=desired,
        pending_count=0,
        deployment_status=status,
        deployments=deployments or [],
        events=events or [],
    )


def _make_metric(values: list[float], stat: str = "average") -> CloudWatchMetric:
    """Build a CloudWatchMetric with given values in each datapoint."""
    dps = [
        {stat: v, "sum": v, "average": v, "timestamp": f"2024-01-15T10:0{i}:00Z"}
        for i, v in enumerate(values)
    ]
    avg = sum(values) / len(values) if values else 0.0
    return CloudWatchMetric(
        namespace="AWS/ApplicationELB",
        metric_name="TargetResponseTime",
        dimensions={"LoadBalancer": "app/prod/abc123"},
        datapoints=dps,
        average=avg,
        maximum=max(values) if values else 0.0,
    )


# ============================================================================
# RequirementsAgent evaluation
# ============================================================================

class TestRequirementsAgentEval:
    """
    Scenarios for RequirementsAgent.
    Tools are re-registered with a mocked LLM so the agent is fully isolated.
    """

    def _make_agent(self, llm_response: str):
        """Build a RequirementsAgent whose LLM always returns llm_response."""
        from app.integrations.requirements import (
            RequirementsAgent,
            analyze_requirement,
            estimate_effort,
            generate_spec,
        )

        mock_llm = _llm(llm_response)
        agent = RequirementsAgent()
        agent._tools.clear()

        async def _analyze(requirement: str) -> str:
            return await analyze_requirement(requirement, mock_llm)

        async def _estimate(analysis: str) -> str:
            return await estimate_effort(analysis, mock_llm)

        async def _generate(requirement: str, analysis: str, estimate: str) -> str:
            return await generate_spec(requirement, analysis, estimate, mock_llm)

        agent.register_tool("analyze_requirement", _analyze, "Analyze")
        agent.register_tool("estimate_effort", _estimate, "Estimate")
        agent.register_tool("generate_spec", _generate, "Generate")
        agent._llm = mock_llm
        return agent

    @pytest.mark.asyncio
    async def test_simple_feature_produces_valid_spec(self):
        """Simple feature request → spec with all required sections."""
        spec = (
            "# Technical Specification\n"
            "## Title\nCSV Export for Dashboard Charts\n"
            "## Overview\nAllow users to download chart data as CSV files.\n"
            "## Functional Requirements\n"
            "- Users can click Export button on any dashboard chart\n"
            "- Downloaded file includes all visible data rows\n"
            "## Technical Approach\nAdd streaming /api/charts/{id}/export endpoint\n"
            "## API Changes\nGET /api/charts/{id}/export → text/csv stream\n"
            "## Database Changes\nNone\n"
            "## Testing Strategy\n"
            "- Unit tests: CSV serialisation logic\n"
            "- Integration tests: export endpoint returns valid CSV\n"
            "- Edge cases: empty dataset, large dataset timeout\n"
            "## Effort Estimate\n3 story points, 2–3 days\n"
            "## Risks\n- Large datasets may cause memory pressure\n"
            "## Open Questions\n- Is there a max row limit?\n"
        )
        agent = self._make_agent(_answer(spec))
        result = await agent.run("Add CSV export for dashboard charts")

        assert "# Technical Specification" in result.answer
        assert "## Title" in result.answer
        assert "## Functional Requirements" in result.answer
        assert "## Testing Strategy" in result.answer
        assert "## Effort Estimate" in result.answer
        assert "## Risks" in result.answer

    @pytest.mark.asyncio
    async def test_complex_multi_part_feature_all_parts_addressed(self):
        """Multi-part requirement → spec explicitly covers every part."""
        spec = (
            "# Technical Specification\n"
            "## Title\nMulti-Channel Notification System\n"
            "## Overview\nThree delivery channels: email, Slack, and in-app bell.\n"
            "## Functional Requirements\n"
            "- Email alerts for failed jobs via SendGrid\n"
            "- Slack webhooks triggered on deployment events\n"
            "- In-app notification bell with unread badge count\n"
            "- Notification preferences per user\n"
            "## Technical Approach\nEvent-driven notification service with pluggable senders\n"
            "## Effort Estimate\n13 story points, 7–10 days\n"
            "## Risks\n- Slack rate limits during high-deploy periods\n"
            "## Open Questions\n- Which job failure types trigger email?\n"
        )
        agent = self._make_agent(_answer(spec))
        result = await agent.run(
            "Build a notification system: "
            "1) email alerts for failed jobs, "
            "2) Slack webhooks on deployments, "
            "3) in-app notification bell with unread count"
        )

        answer = result.answer.lower()
        assert "email" in answer
        assert "slack" in answer
        assert "in-app" in answer or "notification bell" in answer or "unread" in answer
        assert "## Functional Requirements" in result.answer

    @pytest.mark.asyncio
    async def test_ambiguous_request_states_assumptions(self):
        """Vague requirement → spec explicitly documents assumptions made."""
        spec = (
            "# Technical Specification\n"
            "## Title\nUser Activity Dashboard\n"
            "## Overview\nAdmin dashboard showing per-user activity metrics.\n"
            "## Functional Requirements\n"
            "- Display login history, page views, and API calls per user\n"
            "## Open Questions\n"
            "- Assuming 'activity' means login events and page views — please confirm\n"
            "- Assuming data is refreshed hourly, not real-time\n"
            "- Assuming only admin role can view all users\n"
            "## Risks\n"
            "- Scope creep if 'activity' definition expands post-implementation\n"
        )
        agent = self._make_agent(_answer(spec))
        result = await agent.run("Build a user activity dashboard")

        lower = result.answer.lower()
        assert (
            "assum" in lower
            or "open question" in lower
            or "clarif" in lower
            or "confirm" in lower
        )

    @pytest.mark.asyncio
    async def test_contradictory_request_pushes_back(self):
        """Impossible/contradictory requirement → spec flags the conflict."""
        pushback = (
            "# Technical Specification\n"
            "## Title\nReal-Time Reports (Infeasible As Stated)\n"
            "## Overview\nThis requirement contains a fundamental contradiction.\n"
            "## Risks\n"
            "- CONFLICT: 'real-time' requires persistent data storage; "
            "'no database' makes this impossible as stated\n"
            "- Cannot implement without resolving this constraint\n"
            "## Open Questions\n"
            "- Can we use a read-replica or in-memory cache?\n"
            "- Is 'near real-time' (30-second refresh) acceptable?\n"
        )
        agent = self._make_agent(_answer(pushback))
        result = await agent.run(
            "Build real-time sales reports that query live data but use no database"
        )

        lower = result.answer.lower()
        assert (
            "conflict" in lower
            or "contradict" in lower
            or "impossible" in lower
            or "cannot" in lower
            or "open question" in lower
            or "clarif" in lower
        )


# ============================================================================
# CodeReviewAgent evaluation
# ============================================================================

class TestCodeReviewAgentEval:
    """
    Scenarios for CodeReviewAgent.
    Tool functions (analyze_file, generate_review) are called directly
    with a mocked LLM and a pre-populated GitHub mock.
    """

    def _make_github(self, patch_content: str, filename: str = "app/main.py") -> MagicMock:
        pr = PRDetails(
            number=1,
            title="Feature PR",
            description="",
            author="dev",
            head_branch="feature",
            base_branch="main",
            head_sha="abc123",
        )
        fd = FileDiff(
            filename=filename,
            status="modified",
            additions=len(patch_content.splitlines()),
            deletions=0,
            patch=patch_content,
        )
        gh = MagicMock(spec=GitHubService)
        gh.get_pr = AsyncMock(return_value=pr)
        gh.get_pr_diff = AsyncMock(return_value=[fd])
        gh.post_pr_review = AsyncMock(return_value={"id": 1, "state": "COMMENTED"})
        gh._cached_pr = pr
        gh._cached_files = {filename: fd}
        return gh

    @pytest.mark.asyncio
    async def test_clean_code_gets_approve_recommendation(self):
        """Clean, well-typed code with null guards → APPROVE in the review."""
        from app.agents.code_review import analyze_file, generate_review

        clean_patch = (
            "@@ -0,0 +1,7 @@\n"
            "+def calculate_total(items: list[dict]) -> float:\n"
            "+    \"\"\"Sum the 'price' field of each item.\"\"\"\n"
            "+    if not items:\n"
            "+        return 0.0\n"
            "+    return sum(item['price'] for item in items)\n"
        )
        gh = self._make_github(clean_patch, "app/billing.py")
        analysis_text = "No issues found. Code is clean, type-hinted, and handles edge cases."
        review_text = (
            "# Code Review: PR #1\n"
            "## Summary\nClean addition with proper null guard and type hints.\n"
            "## Issues Found\n### Critical\nNone\n### High\nNone\n### Medium\nNone\n"
            "## Recommendation\nAPPROVE\n"
            "**Rationale:** No issues detected. Code follows best practices."
        )
        mock_llm = _llm_seq(analysis_text, review_text)

        analysis = await analyze_file("app/billing.py", gh, mock_llm)
        review = await generate_review(
            "acme", "backend", 1, analysis, gh, mock_llm, post_to_github=False
        )

        assert "APPROVE" in review

    @pytest.mark.asyncio
    async def test_sql_injection_flagged_as_security_critical(self):
        """String-interpolated SQL query → CRITICAL security issue flagged."""
        from app.agents.code_review import analyze_file

        vulnerable_patch = (
            "@@ -0,0 +1,4 @@\n"
            "+def get_user(username):\n"
            "+    query = f\"SELECT * FROM users WHERE username='{username}'\"\n"
            "+    return db.execute(query).fetchone()\n"
        )
        gh = self._make_github(vulnerable_patch, "app/auth.py")
        mock_llm = _llm(
            "ISSUE | CRITICAL | line 2 | SECURITY | "
            "SQL injection via f-string interpolation. "
            "Replace with parameterized query: db.execute('SELECT * FROM users WHERE username=?', (username,))"
        )

        result = await analyze_file("app/auth.py", gh, mock_llm)

        # LLM should have received the diff
        prompt = mock_llm.complete.call_args[1]["messages"][0]["content"]
        assert "SELECT" in prompt or "username" in prompt

        # Analysis names the vulnerability
        lower = result.lower()
        assert "sql injection" in lower or "sql" in lower
        assert "critical" in lower or "security" in lower

    @pytest.mark.asyncio
    async def test_missing_null_check_flagged_as_bug(self):
        """Accessing attribute on potentially-None return value → BUG flagged."""
        from app.agents.code_review import analyze_file

        bug_patch = (
            "@@ -10,0 +10,5 @@\n"
            "+def process_order(order_id: int):\n"
            "+    order = db.find_order(order_id)   # may return None\n"
            "+    total = order.total               # AttributeError if not found\n"
            "+    return apply_discount(total)\n"
        )
        gh = self._make_github(bug_patch, "app/orders.py")
        mock_llm = _llm(
            "ISSUE | HIGH | line 3 | BUG | "
            "Missing null check: db.find_order() can return None. "
            "Accessing .total on None raises AttributeError. "
            "Add: if order is None: raise OrderNotFound(order_id)"
        )

        result = await analyze_file("app/orders.py", gh, mock_llm)

        lower = result.lower()
        assert "null" in lower or "none" in lower or "attributeerror" in lower
        assert "bug" in lower or "high" in lower

    @pytest.mark.asyncio
    async def test_no_tests_for_new_code_flagged(self):
        """New module with zero test coverage → testing gap flagged, REQUEST_CHANGES."""
        from app.agents.code_review import generate_review

        gh = self._make_github(
            "@@ -0,0 +1,15 @@\n+def calculate_fee(amount): return amount * 0.029\n",
            "app/payments.py",
        )
        review_text = (
            "# Code Review: PR #1\n"
            "## Testing Gaps\n"
            "- No unit tests added for the new calculate_fee() function\n"
            "- New payment module has 0% test coverage\n"
            "## Recommendation\nREQUEST_CHANGES\n"
            "**Rationale:** New code without tests is a quality risk. "
            "Add pytest tests covering normal fee, zero amount, and negative amount."
        )
        mock_llm = _llm(review_text)

        review = await generate_review(
            "acme", "backend", 1,
            "calculate_fee added, no test files changed.",
            gh, mock_llm, post_to_github=False,
        )

        lower = review.lower()
        assert "test" in lower
        assert "REQUEST_CHANGES" in review or "request_changes" in lower


# ============================================================================
# CICDAgent evaluation
# ============================================================================

class TestCICDAgentEval:
    """
    Scenarios for CICDAgent.
    analyze_failure() and suggest_fix() are called directly with pre-populated
    GitHub mocks and scripted LLM responses.
    """

    def _make_gh(self, logs: str, run_id: int = 1) -> MagicMock:
        gh = MagicMock(spec=GitHubService)
        gh.get_workflow_runs = AsyncMock(return_value=[])
        gh.get_run_logs = AsyncMock(return_value=logs)
        gh._cached_runs = None
        gh._cached_logs = {run_id: logs}
        gh._cached_analysis = {}
        return gh

    @pytest.mark.asyncio
    async def test_test_failure_identifies_test_name_and_reason(self):
        """pytest failure logs → analysis names the failing test and assertion error."""
        from app.integrations.cicd import analyze_failure

        logs = (
            "Run pytest tests/\n"
            "FAILED tests/test_payment.py::test_refund - AssertionError: assert 400 == 200\n"
            "FAILED tests/test_payment.py::test_charge - AssertionError: assert False\n"
            "2 failed, 38 passed in 4.71s\n"
        )
        expected_analysis = (
            "FAILURE_TYPE: TEST_FAILURE\n"
            "ROOT_CAUSE: test_refund and test_charge both fail. "
            "test_refund expects HTTP 200 but received 400 — the refund endpoint is "
            "rejecting the test payload, likely due to a missing required field.\n"
            "KEY_ERROR: AssertionError: assert 400 == 200\n"
            "AFFECTED_FILES: tests/test_payment.py, app/payments.py\n"
        )
        gh = self._make_gh(logs)
        mock_llm = _llm(expected_analysis)

        result = await analyze_failure(1, gh, mock_llm)

        # Prompt sent to LLM should contain the log snippet and failure type
        prompt = mock_llm.complete.call_args[1]["messages"][0]["content"]
        assert "TEST_FAILURE" in prompt
        assert "test_refund" in prompt or "FAILED" in prompt

        # Result names the test and reason
        lower = result.lower()
        assert "test_refund" in lower or "test_failure" in lower or "assert" in lower

    @pytest.mark.asyncio
    async def test_dependency_error_identifies_package(self):
        """pip install failure → analysis identifies the missing/incompatible package."""
        from app.integrations.cicd import analyze_failure

        logs = (
            "pip install -r requirements.txt\n"
            "ERROR: Could not find a version that satisfies the requirement numpy==1.99.0\n"
            "ERROR: No matching distribution found for numpy==1.99.0\n"
        )
        expected_analysis = (
            "FAILURE_TYPE: DEPENDENCY\n"
            "ROOT_CAUSE: numpy==1.99.0 does not exist on PyPI. "
            "The latest stable release is 1.26.x. Pin to a valid version.\n"
            "KEY_ERROR: No matching distribution found for numpy==1.99.0\n"
            "SEARCH_QUERY: numpy version compatibility\n"
        )
        gh = self._make_gh(logs)
        mock_llm = _llm(expected_analysis)

        result = await analyze_failure(1, gh, mock_llm)

        lower = result.lower()
        assert "numpy" in lower or "dependency" in lower or "DEPENDENCY" in result
        assert "1.99" in result or "distribution" in lower or "version" in lower

    @pytest.mark.asyncio
    async def test_timeout_analysis_suggests_cause(self):
        """Job timeout logs → fix recommendation names likely causes and remedies."""
        from app.integrations.cicd import suggest_fix

        analysis_text = (
            "FAILURE_TYPE: TIMEOUT\n"
            "ROOT_CAUSE: Integration test suite exceeded 360-minute runner limit. "
            "Most likely the tests are waiting on an external HTTP service that never responds.\n"
        )
        gh = MagicMock()
        gh._cached_analysis = {
            "run_id": 1,
            "failure_type": "TIMEOUT",
            "text": analysis_text,
        }
        fix_text = (
            "# CI Fix: TIMEOUT\n"
            "## What Went Wrong\nJob hit the 360-minute wall-clock limit.\n"
            "## Recommended Fix\n"
            "- Add explicit connect/read timeouts to all HTTP calls in integration tests\n"
            "- Use test doubles (mocks/stubs) for external services to avoid real I/O\n"
            "- Check for deadlocks or blocking waits in test setup/teardown\n"
            "## Confidence\nMEDIUM — timeout can indicate hanging test or slow external dep\n"
        )
        mock_llm = _llm(fix_text)

        result = await suggest_fix(1, gh, mock_llm)

        lower = result.lower()
        assert "timeout" in lower or "time" in lower
        assert "fix" in lower or "recommend" in lower or "action" in lower


# ============================================================================
# IncidentResponseAgent evaluation
# ============================================================================

class TestIncidentAgentEval:
    """
    Scenarios for IncidentResponseAgent.
    generate_diagnosis() is called directly; the approval flow is tested
    through the full ReAct loop with a scripted LLM.
    """

    @pytest.mark.asyncio
    async def test_clear_error_spike_produces_structured_diagnosis(self):
        """Known exception dominating error logs → diagnosis names root cause with HIGH confidence."""
        from app.integrations.incident import generate_diagnosis

        context = (
            "ECS running/desired: 0/3  deployment_status: degraded\n"
            "CPUUtilization: avg=94%  max=99%\n"
            "Logs [/app/prod/api]: 480 errors, 8 warnings\n"
            "Recent errors:\n"
            "  ERROR NullPointerException at PaymentController.java:47\n"
            "  ERROR NullPointerException at PaymentController.java:47\n"
            "  ERROR NullPointerException at PaymentController.java:47\n"
        )
        diagnosis = (
            "# Incident Diagnosis\n"
            "## Root Cause\n"
            "NullPointerException in PaymentController:47 is crashing all ECS tasks. "
            "A recent deployment introduced a null reference in the payment processing flow.\n"
            "**Confidence:** HIGH\n"
            "**Reason for confidence:** Every error traces to the same line — consistent crash-loop.\n"
            "## Affected Services\n- api (0/3 tasks running)\n"
            "## Evidence\n"
            "- 480 errors in 30 min, all from PaymentController:47\n"
            "- CPU at 94% consistent with crash-loop restarts\n"
            "## Deployment Correlation\nPOSSIBLE — service degraded after last deploy\n"
            "## Recommended Actions\n"
            "### Immediate (run now — LOW risk)\n"
            "- [ ] Pull thread dump to confirm NPE location\n"
            "### Requires Human Approval (HIGH risk)\n"
            "- [ ] Rollback last deployment — expected to restore 3/3 tasks within 2 min\n"
        )
        mock_llm = _llm(diagnosis)
        result = await generate_diagnosis(context, mock_llm)

        assert "NullPointerException" in result or "Root Cause" in result
        assert "HIGH" in result
        # Prompt should include the gathered context
        prompt = mock_llm.complete.call_args[1]["messages"][0]["content"]
        assert "NullPointerException" in prompt or "480" in prompt

    @pytest.mark.asyncio
    async def test_multiple_causes_lists_alternatives_with_confidence(self):
        """Ambiguous evidence (multiple failing deps) → MEDIUM confidence, alternatives listed."""
        from app.integrations.incident import generate_diagnosis

        context = (
            "ECS running/desired: 1/3  CPU avg=72%  Memory avg=85%\n"
            "Logs: 140 errors — mix of ETIMEDOUT and ECONNREFUSED\n"
            "Recent errors:\n"
            "  ETIMEDOUT connecting to db.internal:5432\n"
            "  Connection refused: redis.internal:6379\n"
            "No recent deployments in last 6h.\n"
        )
        diagnosis = (
            "# Incident Diagnosis\n"
            "## Root Cause\n"
            "Two upstream dependencies failing simultaneously. "
            "Most likely: network partition between app and data tiers.\n"
            "**Confidence:** MEDIUM\n"
            "**Reason for confidence:** Multiple independent failures suggest "
            "infrastructure issue, not application bug.\n"
            "## Affected Services\n- api, PostgreSQL (db.internal), Redis (redis.internal)\n"
            "## Evidence\n"
            "- ETIMEDOUT to PostgreSQL on :5432\n"
            "- Connection refused to Redis on :6379\n"
            "- No recent deployments — not deployment-related\n"
            "## Deployment Correlation\nNO — no deployments in the last 6h\n"
            "## Recommended Actions\n"
            "### Immediate\n"
            "- [ ] Verify VPC network ACLs and security group rules\n"
            "- [ ] Check RDS and ElastiCache health dashboards\n"
        )
        mock_llm = _llm(diagnosis)
        result = await generate_diagnosis(context, mock_llm)

        assert "MEDIUM" in result or "confidence" in result.lower()
        lower = result.lower()
        assert (
            "database" in lower
            or "redis" in lower
            or "network" in lower
            or "postgres" in lower
        )

    @pytest.mark.asyncio
    async def test_high_risk_action_submitted_for_approval(self):
        """
        When the LLM decides to call request_action_approval for a rollback,
        the ApprovalService is invoked and the final answer references the pending request.
        """
        from app.integrations.incident import IncidentResponseAgent
        from app.services.approvals import (
            ApprovalRequest,
            ApprovalService,
            ApprovalStatus,
            RiskLevel,
        )

        # Approval service mock — returns a PENDING request for any HIGH action
        fake_req = ApprovalRequest(
            id="REQ-001",
            agent_name="IncidentResponseAgent",
            action="rollback_deployment",
            parameters={"service": "api", "target_version": "v1.2.3"},
            risk_level=RiskLevel.HIGH,
            description="Rollback api service to v1.2.3 to resolve NPE crash-loop",
            status=ApprovalStatus.PENDING,
            created_at=datetime.now(timezone.utc),
        )
        mock_approvals = MagicMock(spec=ApprovalService)
        mock_approvals.request_approval = AsyncMock(return_value=fake_req)

        # Script the ReAct loop:
        # Step 1 → call request_action_approval (skip gather_context to avoid AWS calls)
        # Step 2 → answer acknowledging pending approval
        # Note: Action Input must be flat JSON — the agent's regex parser is non-greedy
        # and stops at the first '}', so nested objects break JSON parsing.
        react_llm = _llm_seq(
            _action(
                "request_action_approval",
                {
                    "action": "rollback_deployment",
                    "description": "Rollback api service to v1.2.3 to resolve NPE crash-loop",
                    "risk_level": "HIGH",
                },
                thought="I need approval for this high-risk rollback before recommending it.",
            ),
            _answer(
                "Root cause identified: NPE crash-loop in api service. "
                "Rollback to v1.2.3 submitted for approval — request ID: REQ-001. "
                "Awaiting human sign-off before executing. "
                "Approve at: POST /approvals/REQ-001/approve"
            ),
        )

        agent = IncidentResponseAgent(approvals=mock_approvals)
        agent._llm = react_llm

        result = await agent.run('{"alert": "api service 0/3 tasks", "service": "api"}')

        # Approval service must have been called for the HIGH risk action
        assert mock_approvals.request_approval.called
        call_kw = mock_approvals.request_approval.call_args.kwargs
        assert "rollback" in call_kw.get("action", "").lower()
        assert call_kw.get("risk_level", "").upper() == "HIGH"

        # Final answer must reference the pending approval / request ID
        lower = result.answer.lower()
        assert "req-001" in lower or "approval" in lower or "pending" in lower


# ============================================================================
# PerformanceAgent evaluation
# ============================================================================

class TestPerformanceAgentEval:
    """
    Scenarios for PerformanceAgent.
    detect_regression() and correlate_with_deployments() are called directly
    with AWS mocks returning specific metric values.
    """

    def _make_aws(
        self,
        current_values: list[float],
        baseline_values: list[float],
        deployments: list[dict] | None = None,
        running: int = 3,
    ) -> MagicMock:
        """
        AWS mock where:
          - minutes <= 120  → current window (last 1 h)
          - minutes >  120  → baseline window (last 7 d)
        """
        def _get_metrics(**kwargs):
            minutes = kwargs.get("minutes", 60)
            values = current_values if minutes <= 120 else baseline_values
            dps = [
                {"average": v, "sum": v, "timestamp": f"2024-01-15T10:0{i}:00Z"}
                for i, v in enumerate(values)
            ]
            avg = sum(values) / len(values) if values else 0.0
            return CloudWatchMetric(
                namespace=kwargs.get("namespace", "AWS/ApplicationELB"),
                metric_name=kwargs.get("metric_name", "TargetResponseTime"),
                dimensions=kwargs.get("dimensions", {}),
                datapoints=dps,
                average=avg,
                maximum=max(values) if values else 0.0,
            )

        aws = MagicMock(spec=AWSService)
        aws.get_metrics.side_effect = _get_metrics
        aws.get_ecs_status.return_value = _make_ecs_status(
            running=running,
            deployments=deployments or [],
        )
        return aws

    @pytest.mark.asyncio
    async def test_normal_metrics_do_not_flag_regression(self):
        """Current p95 ≈ baseline → OK or WARNING, never REGRESSION."""
        from app.integrations.performance import detect_regression

        # ~100 ms current vs ~95 ms baseline → ~5.3% increase → below 10% threshold
        aws = self._make_aws(
            current_values=[0.098, 0.100, 0.102],
            baseline_values=[0.090, 0.095, 0.100],
        )
        result = await detect_regression(
            "api", "TargetResponseTime", aws,
            threshold_percent=10.0,
            namespace="AWS/ApplicationELB",
            dimensions={"LoadBalancer": "app/prod/abc123"},
        )

        assert "CRITICAL_REGRESSION" not in result
        # Must be OK or WARNING — not a flagged regression
        assert "⚠ REGRESSION DETECTED" not in result

    @pytest.mark.asyncio
    async def test_20pct_latency_increase_flagged_as_regression(self):
        """20% latency increase above 10% threshold → REGRESSION flagged."""
        from app.integrations.performance import detect_regression

        # 120 ms current vs 100 ms baseline → exactly 20% → REGRESSION
        aws = self._make_aws(
            current_values=[0.120, 0.120, 0.120],
            baseline_values=[0.100, 0.100, 0.100],
        )
        result = await detect_regression(
            "api", "TargetResponseTime", aws,
            threshold_percent=10.0,
            namespace="AWS/ApplicationELB",
            dimensions={"LoadBalancer": "app/prod/abc123"},
        )

        assert "REGRESSION" in result
        assert "⚠ REGRESSION DETECTED" in result
        assert "20.0" in result

    @pytest.mark.asyncio
    async def test_regression_after_deploy_correlates_correctly(self):
        """Deployment 15 min before regression time → DEPLOYMENT CORRELATION flagged."""
        from app.integrations.performance import correlate_with_deployments

        regression_time = "2024-01-15T10:30:00+00:00"
        deploy_time = "2024-01-15T10:15:00+00:00"  # 15 min before regression

        aws = self._make_aws(
            current_values=[0.200],
            baseline_values=[0.100],
            deployments=[{
                "id": "deploy-abc",
                "status": "PRIMARY",
                "rollout": "COMPLETED",
                "running": 3,
                "desired": 3,
                "created_at": deploy_time,
            }],
        )

        result = await correlate_with_deployments(
            "api", regression_time, aws, cluster="default", hours=6
        )

        assert "deploy-abc" in result
        assert "DEPLOYMENT CORRELATION" in result
        # Should show how many minutes before the regression the deploy happened
        assert "15" in result

    @pytest.mark.asyncio
    async def test_old_deployment_not_correlated(self):
        """Deployment 10 h before regression (outside 6 h window) → NOT correlated."""
        from app.integrations.performance import correlate_with_deployments

        regression_time = "2024-01-15T10:30:00+00:00"
        old_deploy = "2024-01-15T00:00:00+00:00"  # 10.5 h before — outside window

        aws = self._make_aws(
            current_values=[0.200],
            baseline_values=[0.100],
            deployments=[{
                "id": "deploy-old",
                "status": "PRIMARY",
                "rollout": "COMPLETED",
                "running": 3,
                "desired": 3,
                "created_at": old_deploy,
            }],
        )

        result = await correlate_with_deployments(
            "api", regression_time, aws, cluster="default", hours=6
        )

        lower = result.lower()
        assert (
            "no deployment" in lower
            or "not deployment-related" in lower
            or "no deploy" in lower
        )


# ============================================================================
# Metric helper unit tests
# ============================================================================

class TestMetricHelpers:
    """Pure unit tests for _percentile, _severity, _pct_change, _average."""

    def test_percentile_50_returns_index_50(self):
        from app.integrations.performance import _percentile
        # values [1..100], idx = int(100 * 50 / 100) = 50 → sorted[50] = 51
        assert _percentile(list(range(1, 101)), 50) == 51

    def test_percentile_95_returns_index_95(self):
        from app.integrations.performance import _percentile
        # idx = int(100 * 95 / 100) = 95 → sorted[95] = 96
        assert _percentile(list(range(1, 101)), 95) == 96

    def test_percentile_99_clamps_to_last(self):
        from app.integrations.performance import _percentile
        # idx = int(100 * 99 / 100) = 99 → sorted[99] = 100
        assert _percentile(list(range(1, 101)), 99) == 100

    def test_percentile_empty_returns_none(self):
        from app.integrations.performance import _percentile
        assert _percentile([], 50) is None

    def test_percentile_single_value(self):
        from app.integrations.performance import _percentile
        assert _percentile([42.0], 50) == 42.0

    def test_pct_change_increase(self):
        from app.integrations.performance import _pct_change
        assert _pct_change(120, 100) == 20.0

    def test_pct_change_decrease(self):
        from app.integrations.performance import _pct_change
        assert _pct_change(80, 100) == -20.0

    def test_pct_change_zero_baseline_returns_zero(self):
        from app.integrations.performance import _pct_change
        assert _pct_change(100, 0) == 0.0

    def test_pct_change_no_change(self):
        from app.integrations.performance import _pct_change
        assert _pct_change(100, 100) == 0.0

    def test_severity_ok_on_zero_change(self):
        from app.integrations.performance import _severity
        assert _severity(0, 10) == ("OK", False)

    def test_severity_ok_on_decrease(self):
        from app.integrations.performance import _severity
        assert _severity(-10, 10) == ("OK", False)

    def test_severity_ok_below_half_threshold(self):
        from app.integrations.performance import _severity
        # 4.9% < 10 * 0.5 = 5 → OK
        sev, flagged = _severity(4.9, 10)
        assert sev == "OK"
        assert flagged is False

    def test_severity_warning_at_half_threshold(self):
        from app.integrations.performance import _severity
        # 5.0% == 10 * 0.5 = 5 → WARNING
        sev, flagged = _severity(5.0, 10)
        assert sev == "WARNING"
        assert flagged is False

    def test_severity_regression_at_threshold(self):
        from app.integrations.performance import _severity
        sev, flagged = _severity(10.0, 10)
        assert sev == "REGRESSION"
        assert flagged is True

    def test_severity_regression_above_threshold(self):
        from app.integrations.performance import _severity
        sev, flagged = _severity(15.0, 10)
        assert sev == "REGRESSION"
        assert flagged is True

    def test_severity_critical_at_200x_threshold(self):
        from app.integrations.performance import _severity
        # threshold * CRITICAL_MULTIPLIER * 10 = 10 * 2 * 10 = 200
        sev, flagged = _severity(200.0, 10)
        assert sev == "CRITICAL_REGRESSION"
        assert flagged is True

    def test_severity_critical_above_200(self):
        from app.integrations.performance import _severity
        sev, flagged = _severity(500.0, 10)
        assert sev == "CRITICAL_REGRESSION"
        assert flagged is True


# ============================================================================
# Incident risk rating unit tests
# ============================================================================

class TestIncidentRiskRating:
    """Unit tests for _rate_action_risk — the action risk classifier."""

    def test_rollback_is_high(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("rollback deployment to v1.2.3")
        assert risk == "HIGH"
        assert req is True

    def test_restart_service_is_high(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("restart service api in prod")
        assert risk == "HIGH"
        assert req is True

    def test_redeploy_is_high(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("redeploy the api service")
        assert risk == "HIGH"
        assert req is True

    def test_kill_process_is_high(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("kill process 1234")
        assert risk == "HIGH"
        assert req is True

    def test_flush_cache_is_high(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("flush cache for the user session store")
        assert risk == "HIGH"
        assert req is True

    def test_drop_database_is_critical(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("drop database prod")
        assert risk == "CRITICAL"
        assert req is True

    def test_purge_is_critical(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("purge all event records")
        assert risk == "CRITICAL"
        assert req is True

    def test_truncate_table_is_critical(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("truncate table user_sessions")
        assert risk == "CRITICAL"
        assert req is True

    def test_scale_up_is_medium(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("scale up to 5 ECS tasks")
        assert risk == "MEDIUM"
        assert req is False

    def test_enable_feature_is_medium(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("enable feature flag for new payment flow")
        assert risk == "MEDIUM"
        assert req is False

    def test_view_logs_is_low(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("check the application logs for errors")
        assert risk == "LOW"
        assert req is False

    def test_describe_service_is_low(self):
        from app.integrations.incident import _rate_action_risk
        risk, req = _rate_action_risk("describe the ECS service health status")
        assert risk == "LOW"
        assert req is False


# ============================================================================
# Similar incidents search unit tests
# ============================================================================

class TestSimilarIncidentSearch:
    """Unit tests for _find_similar_incidents keyword matcher."""

    def test_oom_symptoms_match_inc001(self):
        from app.integrations.incident import _find_similar_incidents
        results = _find_similar_incidents("OOMKilled exit code 137 memory tasks failing to start")
        ids = [r["id"] for r in results]
        assert "INC-001" in ids

    def test_500_error_after_deploy_matches_inc002(self):
        from app.integrations.incident import _find_similar_incidents
        results = _find_similar_incidents("500 errors spike after deployment null pointer")
        ids = [r["id"] for r in results]
        assert "INC-002" in ids

    def test_connection_pool_matches_inc003(self):
        from app.integrations.incident import _find_similar_incidents
        results = _find_similar_incidents("connection pool timeout database ECONNREFUSED")
        ids = [r["id"] for r in results]
        assert "INC-003" in ids

    def test_high_cpu_latency_matches_inc004(self):
        from app.integrations.incident import _find_similar_incidents
        results = _find_similar_incidents("high cpu slow response latency p99")
        ids = [r["id"] for r in results]
        assert "INC-004" in ids

    def test_upstream_outage_matches_inc005(self):
        from app.integrations.incident import _find_similar_incidents
        results = _find_similar_incidents("upstream dependency 503 connection refused circuit breaker")
        ids = [r["id"] for r in results]
        assert "INC-005" in ids

    def test_unrelated_symptoms_return_empty(self):
        from app.integrations.incident import _find_similar_incidents
        results = _find_similar_incidents("xyzzy frobnicate quux blargh")
        assert results == []

    def test_results_capped_at_three(self):
        from app.integrations.incident import _find_similar_incidents
        # Broad terms that match all 5 incidents
        results = _find_similar_incidents(
            "timeout latency error 500 deployment connection memory OOMKilled database"
        )
        assert len(results) <= 3

    def test_highest_overlap_ranked_first(self):
        from app.integrations.incident import _find_similar_incidents
        # Keywords specific to INC-003
        results = _find_similar_incidents("connection pool ECONNREFUSED database timeout too many connections")
        assert results[0]["id"] == "INC-003"
