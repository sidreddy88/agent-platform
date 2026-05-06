"""
In-memory store for error events awaiting human approval before entering the pipeline.
Events are added by the scan endpoint and removed when approved or dismissed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


@dataclass
class PendingEvent:
    id: str
    first_line: str       # first line of error message — shown in UI
    service: str
    error_type: str
    log_group: str
    detected_at: str      # ISO string — first time this signature was seen
    last_seen_at: str = ""    # ISO string — most recent occurrence
    occurrences: int = 1      # how many raw matches collapsed into this entry
    handling: str = "unknown"  # "caught" | "uncaught" | "unknown"
    handling_evidence: str = ""  # short snippet that drove the classification
    _event: Any = field(default=None, repr=False)


_ID_PATTERNS = [
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),  # UUIDs
    re.compile(r"\b[0-9a-f]{16,}\b", re.I),                                                  # long hex/IDs
    re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[\dZ:.+\-]*\b"),                       # ISO timestamps
    re.compile(r"\b\d+\b"),                                                                  # bare numbers
]


def _content_sig(event: Any) -> str:
    """Stable content signature for dedup — collapses identical errors regardless of
    stream, task id, or occurrence timestamp.

    Same service + error_type + normalized first line → one pending event. IDs,
    UUIDs, ISO timestamps, and bare numbers are scrubbed before comparison so
    things like "User abc123 not found" and "User def456 not found" merge into
    a single entry whose `occurrences` reflects how often it was seen.
    """
    desc = (getattr(event, "description", "") or getattr(event, "title", "") or "")
    first_line = desc.split("\n")[0]
    normalized = first_line
    for pat in _ID_PATTERNS:
        normalized = pat.sub("X", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()[:200]
    service = (getattr(event, "service", "") or "unknown").lower()
    error_type = (getattr(event, "error_type", "") or "ERROR").upper()
    return f"{service}|{error_type}|{normalized}"


_UNCAUGHT_MARKERS = (
    "traceback (most recent call last)",
    "uncaught exception",
    "uncaughtexception",
    "unhandledpromiserejection",
    "unhandled promise rejection",
    "unhandledrejection",
    "fatal error",
    "[fatal]",
    "process exited",
    "segmentation fault",
    "core dumped",
    "panic:",
)

_CAUGHT_MARKERS = (
    "caught error",
    "caught exception",
    "handled error",
    "fallback used",
    "retrying after error",
    "recovered from error",
    "swallowed error",
    "ignored error",
    "error handler invoked",
)

_PY_FRAME = re.compile(r'File "[^"]+", line \d+')
_JS_FRAME = re.compile(r"\n\s+at\s+\S+")


def classify_handling(message: str) -> tuple[str, str]:
    """Heuristic: did the application code catch this error or did it crash through?

    Returns ``(label, evidence)`` where label is one of ``caught``, ``uncaught``,
    or ``unknown``. ``evidence`` is a short fragment of the message that drove
    the decision — useful to show in the UI tooltip.
    """
    if not message:
        return "unknown", ""
    lower = message.lower()

    for marker in _UNCAUGHT_MARKERS:
        if marker in lower:
            return "uncaught", marker

    py_frames = len(_PY_FRAME.findall(message))
    js_frames = len(_JS_FRAME.findall(message))
    if py_frames >= 2:
        return "uncaught", f"{py_frames} python stack frames"
    if js_frames >= 2:
        return "uncaught", f"{js_frames} stack frames"

    for marker in _CAUGHT_MARKERS:
        if marker in lower:
            return "caught", marker

    if re.search(r"\b(ERROR|WARN|WARNING)\b\s*[:\]]", message) and py_frames == 0 and js_frames == 0:
        return "caught", "logged via error/warn level without stack trace"

    return "unknown", ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PendingEventStore:
    def __init__(self) -> None:
        self._events: dict[str, PendingEvent] = {}
        self._sigs: dict[str, str] = {}          # sig → event_id (pending)
        self._dismissed_sigs: set[str] = set()   # sigs the user has dismissed this session

    def add(self, event: Any) -> Tuple[Optional[PendingEvent], bool]:
        """Add an event or merge into an existing one with the same content signature.

        Returns ``(pending_event, is_new)``. When the signature is dismissed,
        returns ``(None, False)``. When merging into an existing entry, the
        ``occurrences`` counter increments and ``last_seen_at`` updates.
        """
        sig = _content_sig(event)
        if sig in self._dismissed_sigs:
            return None, False

        seen_at = (
            event.detected_at.isoformat()
            if hasattr(event, "detected_at") and hasattr(event.detected_at, "isoformat")
            else _now_iso()
        )

        if sig in self._sigs:
            existing_id = self._sigs[sig]
            existing = self._events.get(existing_id)
            if existing:
                existing.occurrences += 1
                existing.last_seen_at = seen_at
                return existing, False

        description = event.description or event.title or ""
        first_line = description.split("\n")[0].strip()[:250]
        handling, evidence = classify_handling(description)
        pe = PendingEvent(
            id=event.id,
            first_line=first_line,
            service=event.service or "unknown",
            error_type=event.error_type or "ERROR",
            log_group=event.metadata.get("log_group", ""),
            detected_at=seen_at,
            last_seen_at=seen_at,
            occurrences=1,
            handling=handling,
            handling_evidence=evidence,
            _event=event,
        )
        self._events[pe.id] = pe
        self._sigs[sig] = pe.id
        return pe, True

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

    def reset_dismissed(self) -> None:
        """Clear dismissed-sig memory so the next scan surfaces all found events."""
        self._dismissed_sigs.clear()

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
            "last_seen_at": pe.last_seen_at or pe.detected_at,
            "occurrences": pe.occurrences,
            "handling": pe.handling,
            "handling_evidence": pe.handling_evidence,
        }


pending_event_store = PendingEventStore()
