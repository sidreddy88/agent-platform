"""
In-memory store for error events awaiting human approval before entering the pipeline.
Events are added by the scan endpoint and removed when approved or dismissed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass
class PendingEvent:
    id: str
    first_line: str       # first line of error message — shown in UI
    service: str
    error_type: str
    log_group: str
    detected_at: str      # ISO string
    _event: Any           # the full ErrorEvent, not sent to the client


class PendingEventStore:
    def __init__(self) -> None:
        self._events: dict[str, PendingEvent] = {}

    def add(self, event: Any) -> PendingEvent:
        description = event.description or event.title or ""
        first_line = description.split("\n")[0].strip()[:250]
        pe = PendingEvent(
            id=event.id,
            first_line=first_line,
            service=event.service or "unknown",
            error_type=event.error_type or "ERROR",
            log_group=event.metadata.get("log_group", ""),
            detected_at=event.detected_at.isoformat(),
            _event=event,
        )
        self._events[pe.id] = pe
        return pe

    def list_all(self) -> list[PendingEvent]:
        return list(self._events.values())

    def get(self, event_id: str) -> Optional[PendingEvent]:
        return self._events.get(event_id)

    def remove(self, event_id: str) -> Optional[PendingEvent]:
        return self._events.pop(event_id, None)

    def clear(self) -> list[PendingEvent]:
        events = list(self._events.values())
        self._events.clear()
        return events

    def serialize(self, pe: PendingEvent) -> Dict[str, Any]:
        full = getattr(pe._event, "description", "") or ""
        return {
            "id": pe.id,
            "first_line": pe.first_line,
            "full_description": full,
            "service": pe.service,
            "error_type": pe.error_type,
            "log_group": pe.log_group,
            "detected_at": pe.detected_at,
        }


pending_event_store = PendingEventStore()
