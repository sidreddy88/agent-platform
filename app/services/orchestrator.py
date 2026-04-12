"""
MasterOrchestrator — top-level event router that replaces the bare IncidentLoop.

Architecture:
                                 ┌─────────────────────┐
  EventQueue                     │   MasterOrchestrator  │
     │                           │                       │
     └──► _classify(event) ──────►  Routing Table        │
                                 │  (rule-based, no LLM) │
                                 │                       │
                                 │   METRIC_* ──► PerformanceAgent   │
                                 │   CI_/BUILD_ ─► CICDAgent         │
                                 │   everything ─► IncidentLoop      │
                                 │                       │
                                 │   Priority semaphores:│
                                 │   P0 ─── Sem(2)       │
                                 │   P1 ─── Sem(4)       │
                                 │   P2/P3 ─ Sem(6)      │
                                 │                       │
                                 │   Parallel enrichment │
                                 │   (CLOUDWATCH events):│
                                 │   ┌─ IncidentLoop ────┤ gather()
                                 │   └─ DeploymentAgent  │
                                 └─────────────────────────┘

Route decisions are logged to self.route_log (deque[RouteDecision]) for
dashboard visibility via GET /orchestrator/routes.
"""
from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from app.agents.cicd import CICDAgent
from app.agents.deployment import DeploymentAgent
from app.agents.performance import PerformanceAgent
from app.models.events import ErrorEvent, EventSource
from app.services.event_queue import event_queue
from app.services.incident_loop import IncidentLoop
from app.services.alerting import Alert, Severity as AlertSeverity, alerting_service

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing constants
# ---------------------------------------------------------------------------

# Error types that should go to the PerformanceAgent (metric regressions)
_METRIC_PATTERN = re.compile(
    r"^(METRIC_|LATENCY_HIGH|CPU_HIGH|MEMORY_HIGH|ERROR_RATE_HIGH|P95_|P99_)",
    re.IGNORECASE,
)

# Error types from APPLICATION source that route to CICDAgent
_CICD_PATTERN = re.compile(
    r"^(BUILD_|CI_|WORKFLOW_|GITHUB_ACTIONS_|TEST_FAIL)",
    re.IGNORECASE,
)

# Sources that get parallel DeploymentAgent enrichment alongside IncidentLoop
_ENRICHMENT_SOURCES = {EventSource.CLOUDWATCH, EventSource.APPLICATION}

# Concurrency limits per priority lane
_LANE_LIMITS: dict[str, int] = {
    "P0": 2,   # max 2 P0 incidents in parallel — critical path
    "P1": 4,   # max 4 P1 incidents — standard
    "P2": 6,   # max 6 P2/P3 incidents — background
    "P3": 6,
}


# ---------------------------------------------------------------------------
# RouteDecision — logged for dashboard / observability
# ---------------------------------------------------------------------------

@dataclass
class RouteDecision:
    event_id: str
    event_title: str
    service: str
    pipeline: str          # "incident" | "performance" | "cicd"
    priority: str          # "P0" | "P1" | "P2" | "P3"
    enrichment: bool       # whether parallel DeploymentAgent enrichment fired
    dedup_skipped: bool    # True if event was dropped (same key already in-flight)
    routed_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# MasterOrchestrator
# ---------------------------------------------------------------------------

class MasterOrchestrator:
    """
    Routes ErrorEvents to the right pipeline based on event type and priority.

    Replaces the bare IncidentLoop in main.py — this is the single consumer
    of the EventQueue.
    """

    def __init__(self) -> None:
        self._running = False

        # Priority semaphores — completely independent, no cross-lane starvation
        self._semaphores: dict[str, asyncio.Semaphore] = {
            p: asyncio.Semaphore(_LANE_LIMITS[p]) for p in _LANE_LIMITS
        }

        # In-flight deduplication — prevents duplicate pipelines for same error
        self._in_flight: set[str] = set()

        # Sub-orchestrators / agents
        self._incident_loop = IncidentLoop()
        self._deployment_agent = DeploymentAgent()
        self._performance_agent = PerformanceAgent()

        # Route log — last 200 decisions (visible via /orchestrator/routes)
        self.route_log: deque[RouteDecision] = deque(maxlen=200)

        # Stats counters
        self._stats: dict[str, int] = {
            "total_routed": 0,
            "dedup_dropped": 0,
            "incident_pipeline": 0,
            "performance_pipeline": 0,
            "cicd_pipeline": 0,
            "enrichment_fired": 0,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        return {
            **self._stats,
            "in_flight": len(self._in_flight),
            "in_flight_keys": list(self._in_flight),
            "lane_capacity": {
                p: {"limit": _LANE_LIMITS[p], "available": sem._value}
                for p, sem in self._semaphores.items()
            },
        }

    async def run_forever(self) -> None:
        self._running = True
        logger.info("[Orchestrator] Started — draining EventQueue")
        while self._running:
            try:
                event = await asyncio.wait_for(event_queue.dequeue(), timeout=5.0)
                asyncio.create_task(self._dispatch(event))
                event_queue.task_done()
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                logger.error("[Orchestrator] Queue consumer error: %s", exc)

    def stop(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _classify(self, event: ErrorEvent) -> tuple[str, str]:
        """
        Rule-based classification — no LLM, runs in microseconds.

        Returns (pipeline_type, preliminary_priority).
        Preliminary priority is a best-effort estimate before TriageAgent runs.
        """
        error_type = event.error_type or ""
        source = event.source

        # Metric regressions → PerformanceAgent (no code fix, just analysis)
        if _METRIC_PATTERN.match(error_type):
            return "performance", "P2"

        # CI/CD failures from APPLICATION source → CICDAgent
        if source == EventSource.APPLICATION and _CICD_PATTERN.match(error_type):
            return "cicd", "P2"

        # Application / infrastructure errors → full incident pipeline
        # Preliminary priority based on source:
        #   CLOUDWATCH → P1 (ECS app errors tend to be urgent)
        #   DO / Cloudflare → P2 (infra health, less often code-fixable)
        if source == EventSource.CLOUDWATCH:
            return "incident", "P1"

        return "incident", "P2"

    def _dedup_key(self, pipeline: str, event: ErrorEvent) -> str:
        """Unique key for in-flight deduplication."""
        return f"{pipeline}:{event.error_type or event.title}:{event.service}"

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, event: ErrorEvent) -> None:
        """Classify, dedup-check, then run pipeline under the right semaphore."""
        pipeline, priority = self._classify(event)
        dedup_key = self._dedup_key(pipeline, event)

        self._stats["total_routed"] += 1

        # --- Deduplication ---
        if dedup_key in self._in_flight:
            logger.info(
                "[Orchestrator] DEDUP — %s already in-flight for %s/%s, dropping",
                pipeline, event.service, event.error_type,
            )
            self._stats["dedup_dropped"] += 1
            self.route_log.append(RouteDecision(
                event_id=event.id, event_title=event.title, service=event.service,
                pipeline=pipeline, priority=priority, enrichment=False, dedup_skipped=True,
            ))
            return

        # --- Enrichment decision (before semaphore — just a boolean flag) ---
        enrich = pipeline == "incident" and event.source in _ENRICHMENT_SOURCES

        self.route_log.append(RouteDecision(
            event_id=event.id, event_title=event.title, service=event.service,
            pipeline=pipeline, priority=priority, enrichment=enrich, dedup_skipped=False,
        ))
        logger.info(
            "[Orchestrator] → %s lane=%s enrich=%s  '%s'",
            pipeline.upper(), priority, enrich, event.title,
        )

        self._in_flight.add(dedup_key)
        sem = self._semaphores[priority]

        try:
            async with sem:
                await self._run(event, pipeline, enrich)
        except Exception as exc:
            logger.error("[Orchestrator] Pipeline %s failed for %s: %s", pipeline, event.id, exc)
        finally:
            self._in_flight.discard(dedup_key)

    async def _run(self, event: ErrorEvent, pipeline: str, enrich: bool) -> None:
        """Execute the chosen pipeline, with optional parallel enrichment."""
        if pipeline == "incident":
            self._stats["incident_pipeline"] += 1
            if enrich:
                self._stats["enrichment_fired"] += 1
                await self._run_incident_with_enrichment(event)
            else:
                await self._incident_loop._process(event)

        elif pipeline == "performance":
            self._stats["performance_pipeline"] += 1
            await self._run_performance(event)

        elif pipeline == "cicd":
            self._stats["cicd_pipeline"] += 1
            await self._run_cicd(event)

    # ------------------------------------------------------------------
    # Incident pipeline + parallel enrichment
    # ------------------------------------------------------------------

    async def _run_incident_with_enrichment(self, event: ErrorEvent) -> None:
        """
        Run the full incident pipeline AND a parallel DeploymentAgent health check.

        The enrichment result is logged and sent to Slack as additional context.
        It does not block the main pipeline from proceeding.
        """
        main_task = asyncio.create_task(
            self._incident_loop._process(event),
            name=f"incident-{event.id[:8]}",
        )
        enrichment_task = asyncio.create_task(
            self._run_deployment_enrichment(event),
            name=f"enrich-{event.id[:8]}",
        )

        results = await asyncio.gather(main_task, enrichment_task, return_exceptions=True)

        if isinstance(results[0], Exception):
            logger.error("[Orchestrator] Incident pipeline raised: %s", results[0])
        if isinstance(results[1], Exception):
            logger.warning("[Orchestrator] Enrichment raised (non-fatal): %s", results[1])

    async def _run_deployment_enrichment(self, event: ErrorEvent) -> None:
        """
        Parallel: run a DeploymentAgent health check while the incident pipeline runs.
        Result is informational — posted to Slack as extra context.
        """
        prompt = (
            f"Run a health check for the '{event.service}' service. "
            f"A production incident just fired: {event.title}. "
            f"Check ECS task counts, recent log errors, and CPU/memory. "
            f"Return a brief health summary (3-5 bullet points max)."
        )
        try:
            result = await self._deployment_agent.run(prompt)
            logger.info(
                "[Orchestrator] Enrichment for %s complete (%d chars)",
                event.service, len(result.answer),
            )
            await alerting_service.send_alert(Alert(
                severity=AlertSeverity.INFO,
                title=f"[Enrichment] Deployment health for {event.service}",
                message=result.answer[:800],
                source="DeploymentAgent",
                metadata={"event_id": event.id, "service": event.service},
            ))
        except Exception as exc:
            logger.warning("[Orchestrator] Deployment enrichment failed: %s", exc)

    # ------------------------------------------------------------------
    # Performance pipeline
    # ------------------------------------------------------------------

    async def _run_performance(self, event: ErrorEvent) -> None:
        """Route metric regression events to PerformanceAgent."""
        prompt = (
            f"Analyze a metric regression for service '{event.service}'. "
            f"Alert: {event.title}. {event.description}. "
            f"Check p50/p95/p99 latency and error rates, compare to 7-day baseline, "
            f"and flag any regressions."
        )
        try:
            result = await self._performance_agent.run(prompt)
            logger.info(
                "[Orchestrator] PerformanceAgent complete for %s: %s",
                event.service, result.answer[:120],
            )
            await alerting_service.send_alert(Alert(
                severity=AlertSeverity.WARNING,
                title=f"[Performance] Regression analysis: {event.title}",
                message=result.answer[:800],
                source="PerformanceAgent",
                metadata={"event_id": event.id, "service": event.service},
            ))
        except Exception as exc:
            logger.error("[Orchestrator] PerformanceAgent failed: %s", exc)

    # ------------------------------------------------------------------
    # CI/CD pipeline
    # ------------------------------------------------------------------

    async def _run_cicd(self, event: ErrorEvent) -> None:
        """Route CI/CD failure events to CICDAgent."""
        from app.core.config import settings

        repo = event.metadata.get("repo") or settings.fix_target_repo
        prompt = (
            f"A CI/CD pipeline failure was detected in '{repo}'. "
            f"Alert: {event.title}. {event.description}. "
            f"Check recent workflow runs, pull the failure logs, classify the failure type, "
            f"and suggest a fix."
        )
        try:
            agent = CICDAgent()
            result = await agent.run(prompt)
            logger.info(
                "[Orchestrator] CICDAgent complete for %s: %s",
                repo, result.answer[:120],
            )
            await alerting_service.send_alert(Alert(
                severity=AlertSeverity.ERROR,
                title=f"[CI/CD] Failure analysis: {event.title}",
                message=result.answer[:800],
                source="CICDAgent",
                metadata={"event_id": event.id, "repo": repo},
            ))
        except Exception as exc:
            logger.error("[Orchestrator] CICDAgent failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

orchestrator = MasterOrchestrator()
