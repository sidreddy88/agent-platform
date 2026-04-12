"""
Failure injection — creates synthetic ErrorEvents that exercise specific
failure paths through the incident pipeline.

Three scenarios:

  false_positive      A generic, low-signal alert that TriageAgent should
                      classify as "noise" (health-check flap, expected
                      load-test traffic, etc.).  Validates the noise-
                      detection path without creating real incidents.

  duplicate_alert     Two identical ErrorEvents for the same error_type +
                      service fired in quick succession.  Validates the
                      deduplication logic in MasterOrchestrator.

  cascading_failure   A burst of 3–5 related ErrorEvents across different
                      services (simulating a database overload that fans out
                      to downstream services).  Validates the pipeline under
                      concurrent load and priority-lane routing.

Usage (API):
    POST /injection/trigger
    {"scenario": "false_positive"}

Usage (programmatic):
    from app.services.failure_injection import failure_injector
    result = await failure_injector.inject("cascading_failure")
"""
from __future__ import annotations

import asyncio
import logging
from enum import Enum
from typing import Any

from app.models.events import ErrorEvent, EventSource
from app.services.event_queue import event_queue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scenario enum
# ---------------------------------------------------------------------------

class FailureScenario(str, Enum):
    FALSE_POSITIVE   = "false_positive"
    DUPLICATE_ALERT  = "duplicate_alert"
    CASCADING_FAILURE = "cascading_failure"


# ---------------------------------------------------------------------------
# FailureInjector
# ---------------------------------------------------------------------------

class FailureInjector:
    """
    Enqueues synthetic ErrorEvents to trigger specific failure paths.

    Returns a summary dict describing what was injected.
    """

    async def inject(
        self,
        scenario: FailureScenario | str,
        *,
        service: str | None = None,
    ) -> dict[str, Any]:
        """
        Inject a failure scenario.

        Args:
            scenario: One of FailureScenario values.
            service:  Optional service name override (used where applicable).

        Returns:
            A dict with {"scenario", "events_injected", "event_ids", "description"}.
        """
        scenario = FailureScenario(scenario)

        if scenario == FailureScenario.FALSE_POSITIVE:
            return await self._inject_false_positive(service or "health-checker")
        if scenario == FailureScenario.DUPLICATE_ALERT:
            return await self._inject_duplicate_alert(service or "payment-service")
        if scenario == FailureScenario.CASCADING_FAILURE:
            return await self._inject_cascading_failure()

        raise ValueError(f"Unknown scenario: {scenario!r}")

    # ------------------------------------------------------------------
    # Scenario implementations
    # ------------------------------------------------------------------

    async def _inject_false_positive(self, service: str) -> dict[str, Any]:
        """
        A generic health-check timeout that TriageAgent should classify as noise.

        Signals that point to noise:
          - HEALTH_CHECK_TIMEOUT error type (transient by nature)
          - title mentions "intermittent"
          - description references a known maintenance window
        """
        event = ErrorEvent(
            source=EventSource.CLOUDWATCH,
            error_type="HEALTH_CHECK_TIMEOUT",
            title="Intermittent health-check timeout — possible noise",
            description=(
                "A single health-check probe to the /healthz endpoint timed out. "
                "No other signals. Likely transient — occurred during scheduled "
                "maintenance window. No user-facing impact observed."
            ),
            service=service,
        )
        await event_queue.enqueue(event)
        logger.info("[FailureInjector] false_positive → enqueued %s", event.id)
        return {
            "scenario": FailureScenario.FALSE_POSITIVE,
            "events_injected": 1,
            "event_ids": [event.id],
            "description": f"Single HEALTH_CHECK_TIMEOUT for '{service}' — expect TriageAgent to classify as noise",
        }

    async def _inject_duplicate_alert(self, service: str) -> dict[str, Any]:
        """
        Two identical alerts fired 100ms apart — tests dedup in MasterOrchestrator.
        """
        shared_kwargs = dict(
            source=EventSource.APPLICATION,
            error_type="DB_CONNECTION_POOL_EXHAUSTED",
            title="Database connection pool exhausted",
            description=(
                "All connections in the pool are in use. New requests are being "
                "rejected with a timeout after 30s. Root cause: slow query spike."
            ),
            service=service,
        )
        event_a = ErrorEvent(**shared_kwargs)
        event_b = ErrorEvent(**shared_kwargs)

        await event_queue.enqueue(event_a)
        await asyncio.sleep(0.1)   # 100ms gap — same pipeline key → should dedup
        await event_queue.enqueue(event_b)

        logger.info(
            "[FailureInjector] duplicate_alert → enqueued %s + %s (expect dedup)",
            event_a.id, event_b.id,
        )
        return {
            "scenario": FailureScenario.DUPLICATE_ALERT,
            "events_injected": 2,
            "event_ids": [event_a.id, event_b.id],
            "description": (
                f"Two DB_CONNECTION_POOL_EXHAUSTED events for '{service}' fired 100ms apart. "
                "The second should be deduplicated by the orchestrator."
            ),
        }

    async def _inject_cascading_failure(self) -> dict[str, Any]:
        """
        A burst of related failures across 4 services simulating a DB overload
        that fans out to all consumers.

        Fires in quick succession so the orchestrator's priority lanes and
        semaphore limits are exercised.
        """
        cascade: list[tuple[str, str, str]] = [
            ("DB_QUERY_TIMEOUT",         "Database latency spike on primary replica", "postgres-primary"),
            ("CACHE_MISS_RATE_HIGH",     "Redis cache miss rate > 80% — queries hitting DB", "redis-cluster"),
            ("API_RESPONSE_TIME_P99_HIGH","API p99 latency > 5s due to DB contention", "api-gateway"),
            ("QUEUE_DEPTH_HIGH",         "Job queue depth > 10k — workers starved", "worker-pool"),
        ]

        events: list[ErrorEvent] = []
        for error_type, title, service in cascade:
            ev = ErrorEvent(
                source=EventSource.CLOUDWATCH,
                error_type=error_type,
                title=title,
                description=(
                    f"Cascading failure chain started by postgres-primary overload. "
                    f"This {service} alert is a downstream symptom."
                ),
                service=service,
            )
            events.append(ev)
            await event_queue.enqueue(ev)
            await asyncio.sleep(0.05)  # 50ms stagger

        ids = [e.id for e in events]
        logger.info("[FailureInjector] cascading_failure → enqueued %d events %s", len(events), ids)
        return {
            "scenario": FailureScenario.CASCADING_FAILURE,
            "events_injected": len(events),
            "event_ids": ids,
            "description": (
                "4-event cascading failure chain: postgres-primary → redis → api-gateway → worker-pool. "
                "Tests priority-lane routing and concurrent processing under load."
            ),
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

failure_injector = FailureInjector()
