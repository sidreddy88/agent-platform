"""
Tests for MasterOrchestrator — priority-based routing, deduplication, lane limits.

CICDAgent/DeploymentAgent/PerformanceAgent routing (and the already-dead parallel
DeploymentAgent enrichment) were removed from the orchestrator — those agents are
target-integration tooling (app/integrations/) now, not part of the live pipeline
this router drives. Every event goes through the incident pipeline; this file only
tests priority classification, dedup, and lane concurrency.

All external I/O (agents, event queue) is mocked.

Run:
    pytest tests/test_orchestrator.py -v
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

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
    """Fresh orchestrator with the incident loop mocked out."""
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
    }

    incident_loop_mock = MagicMock()
    incident_loop_mock._process = AsyncMock()
    orch._incident_loop = incident_loop_mock

    return orch


# ---------------------------------------------------------------------------
# Priority classification tests
# ---------------------------------------------------------------------------

class TestRouting:
    def test_cloudwatch_error_routes_to_p1(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH, error_type="S3_NO_SUCH_KEY")
        assert orch._classify(event) == "P1"

    def test_application_routes_to_p2(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.APPLICATION, error_type="S3_NO_SUCH_KEY")
        assert orch._classify(event) == "P2"

    def test_digital_ocean_routes_to_p2(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.DIGITAL_OCEAN, error_type="DROPLET_DOWN")
        assert orch._classify(event) == "P2"

    def test_cloudflare_routes_to_p2(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDFLARE, error_type="ERROR_RATE_SPIKE")
        assert orch._classify(event) == "P2"

    def test_event_with_no_error_type_still_classifies(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH, error_type=None)
        assert orch._classify(event) == "P1"


# ---------------------------------------------------------------------------
# Deduplication tests
# ---------------------------------------------------------------------------

class TestDeduplication:
    @pytest.mark.asyncio
    async def test_duplicate_event_is_dropped(self):
        orch = make_orchestrator()
        event = make_event()

        await orch._dispatch(event)
        first_count = orch._incident_loop._process.call_count

        # Manually re-add to in_flight to simulate first event still running
        orch._in_flight.add(orch._dedup_key(event))

        # Second dispatch — should be dropped
        await orch._dispatch(event)

        assert orch._stats["dedup_dropped"] == 1
        assert orch._incident_loop._process.call_count == first_count  # not called again

    @pytest.mark.asyncio
    async def test_different_service_same_error_type_not_deduped(self):
        orch = make_orchestrator()
        event_a = make_event(service="service-a", error_type="S3_NO_SUCH_KEY")
        event_b = make_event(service="service-b", error_type="S3_NO_SUCH_KEY")

        await orch._dispatch(event_a)
        await orch._dispatch(event_b)

        assert orch._stats["dedup_dropped"] == 0
        assert orch._incident_loop._process.call_count == 2

    @pytest.mark.asyncio
    async def test_in_flight_key_cleared_after_pipeline(self):
        orch = make_orchestrator()
        event = make_event()

        await orch._dispatch(event)

        key = orch._dedup_key(event)
        assert key not in orch._in_flight  # cleaned up after pipeline finishes


# ---------------------------------------------------------------------------
# Pipeline dispatch tests
# ---------------------------------------------------------------------------

class TestPipelineDispatch:
    @pytest.mark.asyncio
    async def test_cloudwatch_incident_runs_incident_pipeline(self):
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH, error_type="S3_NO_SUCH_KEY")

        await orch._dispatch(event)

        assert orch._stats["incident_pipeline"] == 1
        orch._incident_loop._process.assert_called_once()


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

        await orch._dispatch(event)

        assert orch._semaphores["P1"]._value == initial_value

    @pytest.mark.asyncio
    async def test_semaphore_released_after_pipeline_failure(self):
        """Semaphore is always released even when pipeline raises."""
        orch = make_orchestrator()
        event = make_event(source=EventSource.CLOUDWATCH)
        orch._incident_loop._process = AsyncMock(side_effect=RuntimeError("crash"))

        initial_value = orch._semaphores["P1"]._value

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

        await orch._dispatch(event)

        assert len(orch.route_log) == 1
        decision = orch.route_log[0]
        assert decision.event_id == event.id
        assert decision.priority == "P1"
        assert decision.dedup_skipped is False

    @pytest.mark.asyncio
    async def test_dedup_decision_logged(self):
        orch = make_orchestrator()
        event = make_event()
        orch._in_flight.add(orch._dedup_key(event))

        await orch._dispatch(event)

        assert orch.route_log[-1].dedup_skipped is True

    @pytest.mark.asyncio
    async def test_stats_incremented_correctly(self):
        orch = make_orchestrator()

        events = [
            make_event(source=EventSource.CLOUDWATCH, error_type="S3_NO_SUCH_KEY", service="a"),
            make_event(source=EventSource.APPLICATION, error_type="OTHER_ERROR", service="b"),
        ]

        for e in events:
            await orch._dispatch(e)

        assert orch._stats["total_routed"] == 2
        assert orch._stats["incident_pipeline"] == 2
