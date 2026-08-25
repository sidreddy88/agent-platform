"""
MasterOrchestrator — top-level event router that replaces the bare IncidentLoop.

Architecture:
                                 ┌─────────────────────┐
  EventQueue                     │   MasterOrchestrator  │
     │                           │                       │
     └──► _classify(event) ──────►  Routing Table        │
                                 │  (rule-based, no LLM) │
                                 │                       │
                                 │   everything ─► IncidentLoop      │
                                 │                       │
                                 │   Priority semaphores:│
                                 │   P0 ─── Sem(2)       │
                                 │   P1 ─── Sem(4)       │
                                 │   P2/P3 ─ Sem(6)      │
                                 └─────────────────────────┘

CICDAgent/DeploymentAgent/PerformanceAgent used to have their own routes here
(METRIC_* -> PerformanceAgent, CI_/BUILD_ -> CICDAgent) plus a parallel
DeploymentAgent enrichment alongside every CLOUDWATCH incident. Removed --
those agents are target-integration tooling (app/integrations/), not part of
the core pipeline this router drives, and the enrichment path was already
dead code in production (its call site was commented out). See DECISIONS.md.

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

from app.models.events import ErrorEvent, EventSource
from app.services.event_queue import event_queue
from app.services.incident_loop import IncidentLoop

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Routing constants
# ---------------------------------------------------------------------------

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
    priority: str          # "P0" | "P1" | "P2" | "P3"
    dedup_skipped: bool    # True if event was dropped (same key already in-flight)
    routed_at: datetime = field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# MasterOrchestrator
# ---------------------------------------------------------------------------

class MasterOrchestrator:
    """
    Dispatches ErrorEvents to the incident pipeline under a priority-lane
    semaphore, with in-flight dedup.

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

        # Route log — last 200 decisions (visible via /orchestrator/routes)
        self.route_log: deque[RouteDecision] = deque(maxlen=200)

        # Stats counters
        self._stats: dict[str, int] = {
            "total_routed": 0,
            "dedup_dropped": 0,
            "incident_pipeline": 0,
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

    def _classify(self, event: ErrorEvent) -> str:
        """
        Rule-based priority estimate — no LLM, runs in microseconds.

        Every event goes through the incident pipeline; this only estimates
        priority before TriageAgent runs.
        Preliminary priority based on source:
          CLOUDWATCH → P1 (ECS app errors tend to be urgent)
          DO / Cloudflare / everything else → P2 (less often code-fixable)
        """
        if event.source == EventSource.CLOUDWATCH:
            return "P1"
        return "P2"

    def _dedup_key(self, event: ErrorEvent) -> str:
        """Unique key for in-flight deduplication — normalizes variable tokens so same error with different values deduplicates."""
        desc = re.sub(r'\b[a-f0-9]{8,}\b|\b\d+[a-zA-Z]*\b', 'X', (event.description or "")[:80]).strip()
        return f"{event.error_type or event.title}:{event.service}:{desc}"

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, event: ErrorEvent) -> None:
        """Classify, dedup-check, then run the incident pipeline under the right semaphore."""
        priority = self._classify(event)
        dedup_key = self._dedup_key(event)

        self._stats["total_routed"] += 1

        # --- Deduplication ---
        if dedup_key in self._in_flight:
            logger.info(
                "[Orchestrator] DEDUP — already in-flight for %s/%s, dropping",
                event.service, event.error_type,
            )
            self._stats["dedup_dropped"] += 1
            self.route_log.append(RouteDecision(
                event_id=event.id, event_title=event.title, service=event.service,
                priority=priority, dedup_skipped=True,
            ))
            return

        self.route_log.append(RouteDecision(
            event_id=event.id, event_title=event.title, service=event.service,
            priority=priority, dedup_skipped=False,
        ))
        logger.info("[Orchestrator] → lane=%s '%s'", priority, event.title)

        self._in_flight.add(dedup_key)
        sem = self._semaphores[priority]

        try:
            async with sem:
                self._stats["incident_pipeline"] += 1
                await self._incident_loop._process(event)
        except Exception as exc:
            logger.error("[Orchestrator] Incident pipeline failed for %s: %s", event.id, exc)
        finally:
            self._in_flight.discard(dedup_key)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

orchestrator = MasterOrchestrator()
