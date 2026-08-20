"""
Tests for MasterOrchestrator — routing, priority lanes, deduplication, parallel enrichment.

All external I/O (agents, alerting, event queue) is mocked.

Run:
    pytest tests/test_orchestrator.py -v
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.events import ErrorEvent, EventSource
from app.services.orchestrator import MasterOrchestrator

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def make_event(
    source=EventSource.CLOUDWATCH,
    error_type="S3_NO_SUCH_KEY",
    service="image-service",
    title="NoSuchKey error",
    **meta,
) -> ErrorEvent:
    return ErrorEvent(
        source=source,
        error_type=error_type,
        title=title,
        description="test event",
        service=service,
        metadata=meta,
    )


def make_orchestrator() -> MasterOrchestrator:
    """Fresh orchestrator with all sub-agents mocked out."""
    orch = MasterOrchestrator.__new__(MasterOrchestrator)
    orch._running = False
    orch._in_flight = set()
    orch._semaphores = {
        "P0": asyncio.Semaphore(2),
        "P1": asyncio.Semaphore(4),
        "P2": asyncio.Semaphore(6),
        "P3": asyncio.Semaphore(6),
    }

    from collections import deque
    orch.route_log = deque(maxlen=200)
    orch._stats = {
        "total_routed": 0,
        "dedup_dropped": 0,
        "incident_pipeline": 0,
        "performance_pipeline": 0,
        "cicd_pipeline": 0,
        "enrichment_fired": 0,
    }

    # Mock sub-components
    from app.agents.base import AgentResult
    agent_ok = MagicMock()
    agent_ok.run = AsyncMock(return_value=AgentResult(answer="ok", steps=[], iterations=1))

    incident_loop_mock = MagicMock()
    incident_loop_mock._process = AsyncMock()

    orch._incident_loop = incident_loop_mock
    orch._deployment_agent = agent_ok
    orch._performance_agent = MagicMock()
    orch._performance_agent.run = AsyncMock(return_value=AgentResult(answer="no regression", steps=[], iterations=1))

    return orch


# ---------------------------------------------------------------------------
# Routing tests
# ---------------------------------------------------------------------------

class TestRouting:
    def test_cloudwatch_error_routes_to_incident_p1(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH, error_type="S3_NO_SUCH_KEY")
        pipeline, priority = orch._classify(event)
        assert pipeline == "incident"
        assert priority == "P1"

    def test_metric_error_routes_to_performance(self):
        orch = make_orchestrator()
        for error_type in ["METRIC_LATENCY", "LATENCY_HIGH", "CPU_HIGH", "MEMORY_HIGH", "ERROR_RATE_HIGH", "P95_SPIKE"]:
            pipeline, priority = orch._classify(make_event(error_type=error_type))
            assert pipeline == "performance", f"Expected performance for {error_type}"
            assert priority == "P2"

    def test_cicd_failure_from_application_routes_to_cicd(self):
        orch = make_orchestrator()
        for error_type in ["BUILD_FAILED", "CI_FAILURE", "WORKFLOW_ERROR", "GITHUB_ACTIONS_FAIL", "TEST_FAIL"]:
            event = make_event(source=EventSource.APPLICATION, error_type=error_type)
            pipeline, priority = orch._classify(event)
            assert pipeline == "cicd", f"Expected cicd for {error_type}"
            assert priority == "P2"

    def test_application_non_cicd_routes_to_incident(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.APPLICATION, error_type="S3_NO_SUCH_KEY")
        pipeline, priority = orch._classify(event)
        assert pipeline == "incident"
        assert priority == "P2"

    def test_digital_ocean_routes_to_incident_p2(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.DIGITAL_OCEAN, error_type="DROPLET_DOWN")
        pipeline, priority = orch._classify(event)
        assert pipeline == "incident"
        assert priority == "P2"

    def test_cloudflare_routes_to_incident_p2(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDFLARE, error_type="ERROR_RATE_SPIKE")
        pipeline, priority = orch._classify(event)
        # error_type doesn't match _METRIC_PATTERN prefix, routes to incident
        assert pipeline == "incident"
        assert priority == "P2"

    def test_event_with_no_error_type_routes_to_incident(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH, error_type=None)
        pipeline, priority = orch._classify(event)
        assert pipeline == "incident"


# ---------------------------------------------------------------------------
# Enrichment flag tests
# ---------------------------------------------------------------------------

class TestEnrichmentFlag:
    def test_cloudwatch_incident_gets_enrichment(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH, error_type="S3_NO_SUCH_KEY")
        pipeline, _ = orch._classify(event)
        enrich = pipeline == "incident" and event.source in {EventSource.CLOUDWATCH, EventSource.APPLICATION}
        assert enrich is True

    def test_digital_ocean_incident_no_enrichment(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.DIGITAL_OCEAN, error_type="DROPLET_DOWN")
        pipeline, _ = orch._classify(event)
        enrich = pipeline == "incident" and event.source in {EventSource.CLOUDWATCH, EventSource.APPLICATION}
        assert enrich is False

    def test_performance_pipeline_never_enriched(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH, error_type="METRIC_LATENCY")
        pipeline, _ = orch._classify(event)
        enrich = pipeline == "incident" and event.source in {EventSource.CLOUDWATCH, EventSource.APPLICATION}
        assert enrich is False


# ---------------------------------------------------------------------------
# Deduplication tests
# ---------------------------------------------------------------------------

class TestDeduplication:
    @pytest.mark.asyncio
    async def test_duplicate_event_is_dropped(self):
        orch = make_orchestrator()
        event = make_event()

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            # First dispatch
            await orch._dispatch(event)
            first_count = orch._incident_loop._process.call_count

            # Manually re-add to in_flight to simulate first event still running
            orch._in_flight.add(orch._dedup_key("incident", event))

            # Second dispatch — should be dropped
            await orch._dispatch(event)

        assert orch._stats["dedup_dropped"] == 1
        assert orch._incident_loop._process.call_count == first_count  # not called again

    @pytest.mark.asyncio
    async def test_different_service_same_error_type_not_deduped(self):
        orch = make_orchestrator()
        event_a = make_event(service="service-a", error_type="S3_NO_SUCH_KEY")
        event_b = make_event(service="service-b", error_type="S3_NO_SUCH_KEY")

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event_a)
            await orch._dispatch(event_b)

        assert orch._stats["dedup_dropped"] == 0
        assert orch._incident_loop._process.call_count == 2

    @pytest.mark.asyncio
    async def test_in_flight_key_cleared_after_pipeline(self):
        orch = make_orchestrator()
        event = make_event()

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event)

        key = orch._dedup_key("incident", event)
        assert key not in orch._in_flight  # cleaned up after pipeline finishes


# ---------------------------------------------------------------------------
# Pipeline dispatch tests
# ---------------------------------------------------------------------------

class TestPipelineDispatch:
    @pytest.mark.asyncio
    async def test_cloudwatch_incident_runs_incident_pipeline(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH, error_type="S3_NO_SUCH_KEY")

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event)

        assert orch._stats["incident_pipeline"] == 1
        assert orch._stats["performance_pipeline"] == 0
        assert orch._stats["cicd_pipeline"] == 0

    @pytest.mark.asyncio
    async def test_metric_event_runs_performance_pipeline(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH, error_type="METRIC_LATENCY")

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event)

        assert orch._stats["performance_pipeline"] == 1
        assert orch._stats["incident_pipeline"] == 0
        orch._incident_loop._process.assert_not_called()

    @pytest.mark.asyncio
    async def test_cicd_event_runs_cicd_pipeline(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.APPLICATION, error_type="BUILD_FAILED")

        from app.agents.base import AgentResult
        cicd_mock = MagicMock()
        cicd_mock.run = AsyncMock(return_value=AgentResult(answer="build fixed", steps=[], iterations=1))

        with (
            patch("app.services.orchestrator.alerting_service") as mock_alert,
            patch("app.services.orchestrator.CICDAgent", return_value=cicd_mock),
        ):
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event)

        assert orch._stats["cicd_pipeline"] == 1
        assert orch._stats["incident_pipeline"] == 0

    @pytest.mark.asyncio
    async def test_cloudwatch_incident_does_not_fire_enrichment(self):
        """Parallel deployment enrichment is deliberately disabled (see the
        commented-out block in Orchestrator._run) -- CLOUDWATCH incidents
        still route to the incident pipeline, they just don't also fire
        deployment enrichment alongside it anymore."""
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH, error_type="S3_NO_SUCH_KEY")

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event)

        assert orch._stats["incident_pipeline"] == 1
        assert orch._stats["enrichment_fired"] == 0
        orch._deployment_agent.run.assert_not_called()

    @pytest.mark.asyncio
    async def test_digital_ocean_incident_no_enrichment(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.DIGITAL_OCEAN, error_type="DROPLET_DOWN")

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event)

        assert orch._stats["enrichment_fired"] == 0
        orch._deployment_agent.run.assert_not_called()


# ---------------------------------------------------------------------------
# Parallel enrichment tests
# ---------------------------------------------------------------------------

class TestParallelEnrichment:
    @pytest.mark.asyncio
    async def test_incident_and_enrichment_run_concurrently(self):
        """Both main pipeline and enrichment agent are awaited together."""
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH)

        call_order = []

        async def mock_process(ev):
            call_order.append("incident")

        async def mock_enrich(prompt):
            call_order.append("enrichment")
            from app.agents.base import AgentResult
            return AgentResult(answer="healthy", steps=[], iterations=1)

        orch._incident_loop._process = mock_process
        orch._deployment_agent.run = mock_enrich

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._run_incident_with_enrichment(event)

        # Both ran
        assert "incident" in call_order
        assert "enrichment" in call_order

    @pytest.mark.asyncio
    async def test_enrichment_failure_does_not_break_incident_pipeline(self):
        """If DeploymentAgent raises, the incident pipeline still completes."""
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH)

        incident_ran = []

        async def mock_process(ev):
            incident_ran.append(True)

        orch._incident_loop._process = mock_process
        orch._deployment_agent.run = AsyncMock(side_effect=RuntimeError("AWS timeout"))

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            # Should not raise even though enrichment fails
            await orch._run_incident_with_enrichment(event)

        assert incident_ran  # main pipeline ran

    @pytest.mark.asyncio
    async def test_incident_failure_logged_not_raised(self):
        """If the incident pipeline raises, _run_incident_with_enrichment handles it."""
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH)

        orch._incident_loop._process = AsyncMock(side_effect=RuntimeError("triage timeout"))
        orch._deployment_agent.run = AsyncMock(
            return_value=MagicMock(answer="healthy")
        )

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            # Should not propagate exception
            await orch._run_incident_with_enrichment(event)


# ---------------------------------------------------------------------------
# Priority lane tests
# ---------------------------------------------------------------------------

class TestPriorityLanes:
    @pytest.mark.asyncio
    async def test_p0_uses_separate_semaphore_from_p2(self):
        """P0 and P2 events never share semaphore slots."""
        orch = make_orchestrator()
        p0_sem = orch._semaphores["P0"]
        p2_sem = orch._semaphores["P2"]
        assert p0_sem is not p2_sem

    @pytest.mark.asyncio
    async def test_semaphore_released_after_pipeline(self):
        """Semaphore is always released after pipeline completes (happy path)."""
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH)

        initial_value = orch._semaphores["P1"]._value

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event)

        assert orch._semaphores["P1"]._value == initial_value

    @pytest.mark.asyncio
    async def test_semaphore_released_after_pipeline_failure(self):
        """Semaphore is always released even when pipeline raises."""
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH)
        orch._incident_loop._process = AsyncMock(side_effect=RuntimeError("crash"))

        initial_value = orch._semaphores["P1"]._value

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event)

        assert orch._semaphores["P1"]._value == initial_value

    @pytest.mark.asyncio
    async def test_concurrent_events_respect_lane_limits(self):
        """
        With P1 lane limit=4, firing 6 P1 events concurrently means max 4 run
        simultaneously — the others wait.
        """
        orch = make_orchestrator()
        # Reduce to limit=2 so the test is fast
        orch._semaphores["P1"] = asyncio.Semaphore(2)

        running_concurrently = []
        max_concurrent = []

        async def slow_process(ev):
            running_concurrently.append(1)
            max_concurrent.append(len(running_concurrently))
            await asyncio.sleep(0.01)
            running_concurrently.pop()

        orch._incident_loop._process = slow_process

        events = [
            make_event(source=EventSource.CLOUDWATCH, error_type=f"ERR_{i}", service=f"svc-{i}")
            for i in range(6)
        ]

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            tasks = [asyncio.create_task(orch._dispatch(e)) for e in events]
            await asyncio.gather(*tasks)

        assert max(max_concurrent) <= 2  # never exceeded the lane limit


# ---------------------------------------------------------------------------
# Route log tests
# ---------------------------------------------------------------------------

class TestRouteLog:
    @pytest.mark.asyncio
    async def test_routing_decision_logged(self):
        orch = make_orchestrator()
        event = make_event()

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event)

        assert len(orch.route_log) == 1
        decision = orch.route_log[0]
        assert decision.event_id == event.id
        assert decision.pipeline == "incident"
        assert decision.priority == "P1"
        assert decision.dedup_skipped is False

    @pytest.mark.asyncio
    async def test_dedup_decision_logged(self):
        orch = make_orchestrator()
        event = make_event()
        orch._in_flight.add(orch._dedup_key("incident", event))

        with patch("app.services.orchestrator.alerting_service") as mock_alert:
            mock_alert.send_alert = AsyncMock()
            await orch._dispatch(event)

        assert orch.route_log[-1].dedup_skipped is True

    @pytest.mark.asyncio
    async def test_stats_incremented_correctly(self):
        orch = make_orchestrator()

        events = [
            make_event(source=EventSource.CLOUDWATCH, error_type="S3_NO_SUCH_KEY", service="a"),
            make_event(source=EventSource.CLOUDWATCH, error_type="METRIC_LATENCY", service="b"),
            make_event(source=EventSource.APPLICATION, error_type="BUILD_FAILED", service="c"),
        ]

        from app.agents.base import AgentResult
        cicd_mock = MagicMock()
        cicd_mock.run = AsyncMock(return_value=AgentResult(answer="ok", steps=[], iterations=1))

        with (
            patch("app.services.orchestrator.alerting_service") as mock_alert,
            patch("app.services.orchestrator.CICDAgent", return_value=cicd_mock),
        ):
            mock_alert.send_alert = AsyncMock()
            for e in events:
                await orch._dispatch(e)

        assert orch._stats["total_routed"] == 3
        assert orch._stats["incident_pipeline"] == 1
        assert orch._stats["performance_pipeline"] == 1
        assert orch._stats["cicd_pipeline"] == 1
        # Parallel deployment enrichment is deliberately disabled (see the
        # commented-out block in Orchestrator._run) -- never fires currently.
        assert orch._stats["enrichment_fired"] == 0
