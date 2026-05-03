"""
In-memory store for error events awaiting human approval before entering the pipeline.
Events are added by the scan endpoint and removed when approved or dismissed.
"""
from __future__ import annotations

import re
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


def _content_sig(event: Any) -> str:
    """Stable content signature for dedup — same error across scan runs has same sig."""
    desc = event.description or event.title or ""
    normalized = re.sub(r"\b\d+\b", "N", desc[:120]).strip()
    return f"{event.service}|{event.error_type}|{normalized[:80]}"


class PendingEventStore:
    def __init__(self) -> None:
        self._events: dict[str, PendingEvent] = {}
        self._sigs: dict[str, str] = {}          # sig → event_id (pending)
        self._dismissed_sigs: set[str] = set()   # sigs the user has dismissed this session

    def add(self, event: Any) -> Optional[PendingEvent]:
        description = event.description or event.title or ""
        first_line = description.split("\n")[0].strip()[:250]

        sig = _content_sig(event)
        if sig in self._dismissed_sigs:
            return None

        # Return existing pending event for same content
        if sig in self._sigs:
            existing_id = self._sigs[sig]
            if existing_id in self._events:
                return self._events[existing_id]

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
        self._sigs[sig] = pe.id
        return pe

    def list_all(self) -> list[PendingEvent]:
        return list(self._events.values())

    def get(self, event_id: str) -> Optional[PendingEvent]:
        return self._events.get(event_id)

    def remove(self, event_id: str) -> Optional[PendingEvent]:
        """Remove event without marking it dismissed (used for approve path)."""
        pe = self._events.pop(event_id, None)
        if pe:
            sig = _content_sig(pe._event)
            self._sigs.pop(sig, None)
        return pe

    def dismiss(self, event_id: str) -> Optional[PendingEvent]:
        """Remove event and prevent its content from resurfacing this session."""
        pe = self._events.pop(event_id, None)
        if pe:
            sig = _content_sig(pe._event)
            self._sigs.pop(sig, None)
            self._dismissed_sigs.add(sig)
        return pe

    def clear(self) -> list[PendingEvent]:
        events = list(self._events.values())
        self._events.clear()
        self._sigs.clear()
        self._dismissed_sigs.clear()
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
