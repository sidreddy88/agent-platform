"""
Auto-triage: run TriageAgent on each new pending event as soon as detection finds it.

Triage used to run only inside the incident pipeline, after a human approved the event on the dashboard. When
nobody approved, nothing was triaged: on 2026-11-05 detection found 14 new events in a day and triage ran on none,
so production triage traces (which the weekly monitoring and the harness self-improvement loop learn from) dried
up. Triage is cheap and only classifies, so it now runs before approval; diagnosis, fixes and PRs still wait for a
human. The result is stored on the pending event (shown by GET /events/pending). An approved event goes through
the pipeline as before, which triages it again with fresh counts.

Runs in the background, one triage at a time, so a burst of new events can't stall detection or flood the API.
A failure is logged and leaves the event untriaged; it never affects detection or approval.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from app.services.pending_events import PendingEvent

logger = logging.getLogger(__name__)


class AutoTriage:
    def __init__(self, concurrency: int = 1) -> None:
        self._gate = asyncio.Semaphore(concurrency)
        self._agent = None
        self._tasks: set[asyncio.Task] = set()

    def _triage_agent(self):
        if self._agent is None:
            from app.agents.triage import TriageAgent
            from app.services.llm_gateway import llm_gateway

            self._agent = TriageAgent(llm=llm_gateway.get_llm_service_for("triage"))
        return self._agent

    def schedule(self, pe: PendingEvent) -> asyncio.Task:
        """Triage `pe` in the background. Keeps a reference so the task isn't garbage-collected mid-run."""
        task = asyncio.create_task(self.run(pe))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def run(self, pe: PendingEvent) -> None:
        from app.api.websocket_dashboard import broadcast
        from app.services.pending_events import pending_event_store

        async with self._gate:
            if pending_event_store.get(pe.id) is None:        # approved or dismissed while waiting: the pipeline
                return                                         # (or nobody) needs this triage now
            try:
                result = await self._triage_agent().triage(pe._event)
            except Exception as exc:  # noqa: BLE001 - never let triage break detection
                logger.warning("[AutoTriage] triage failed for pending event %s: %s", pe.id, exc)
                return
        pe.triage = {
            "decision": result.decision,
            "severity": result.severity,
            "occurrences_24h": result.occurrences_24h,
            "duplicate_pr": result.duplicate_pr,
            "reasoning": result.reasoning,
            "triaged_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info("[AutoTriage] %s -> %s/%s: %s", pe.id, result.decision, result.severity, pe.first_line[:100])
        if pending_event_store.get(pe.id) is not None:
            await broadcast({"type": "pending_event_updated", "event": pending_event_store.serialize(pe)})


auto_triage = AutoTriage()
