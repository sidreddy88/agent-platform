"""
Tests for _has_active_incident() — the scan-time gate that decides whether a
crash signature should be re-queued.

Real production bug: incidents for a previewCode crash were deleted (wanting a
fresh look), the crash recurred, but a 4-week crash scan still reported "no new
crashes detected" — the scan's only memory was pending_events (in-memory,
independent of incident_store, and wiped by every deploy). This tests the fix:
suppression is now keyed off whether an incident *currently exists* in
incident_store for the same signature, not off pending_events' own state.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.api.routes.incidents import _has_active_incident
from app.models.events import ErrorEvent, EventSource, IncidentState, IncidentStatus
from app.services.incident_store import IncidentStore


def _store(incidents: list[IncidentState]) -> IncidentStore:
    store = IncidentStore.__new__(IncidentStore)
    store._incidents = {i.id: i for i in incidents}
    store._monitor_pr_map = {}
    return store


def _crash_event(desc: str = "CastError: cast to Number failed for value \"8935194)를\"") -> ErrorEvent:
    return ErrorEvent(
        source=EventSource.CLOUDWATCH,
        error_type="CASTERROR",
        title="CASTERROR in TaskTargetApp",
        description=desc,
        service="TaskTargetApp",
        metadata={"log_group": "/ecs/TaskTargetApp", "task_id": "t1", "timestamp": 1000},
    )


def _incident_for(event: ErrorEvent, status: IncidentStatus) -> IncidentState:
    inc = IncidentState(error_event=event)
    inc.status = status
    return inc


def test_no_matching_incident_is_not_active():
    with patch("app.api.routes.incidents.incident_store", _store([])):
        assert _has_active_incident(_crash_event()) is False


@pytest.mark.parametrize(
    "status",
    [
        IncidentStatus.AWAITING_APPROVAL,
        IncidentStatus.FIXING,
        IncidentStatus.RESOLVED,
    ],
)
def test_matching_incident_of_any_status_suppresses_rescan(status):
    """Both 'already fixed' (RESOLVED) and 'actively in flight' must suppress —
    this is the exact fix requested: don't spam duplicates while one's already
    being worked, and don't resurface once a PR has actually merged for it."""
    event = _crash_event()
    existing = _incident_for(_crash_event(), status)
    with patch("app.api.routes.incidents.incident_store", _store([existing])):
        assert _has_active_incident(event) is True


def test_deleted_incident_lets_the_signature_resurface():
    """The regression this PR fixes: once the matching incident is gone from
    incident_store (deleted by a human wanting a fresh look), the exact same
    crash signature must be treated as new again on the next scan."""
    event = _crash_event()
    existing = _incident_for(_crash_event(), IncidentStatus.AWAITING_APPROVAL)

    with patch("app.api.routes.incidents.incident_store", _store([existing])):
        assert _has_active_incident(event) is True

    # Incident deleted — store no longer contains a match.
    with patch("app.api.routes.incidents.incident_store", _store([])):
        assert _has_active_incident(event) is False


def test_different_signature_does_not_suppress():
    """A different crash entirely (different service/error_type/message) must
    not be swallowed just because some unrelated incident exists."""
    existing = _incident_for(
        ErrorEvent(
            source=EventSource.CLOUDWATCH,
            error_type="TYPEERROR",
            title="TYPEERROR in OtherTask",
            description="TypeError: cannot read property 'x'",
            service="OtherTask",
            metadata={"log_group": "/ecs/OtherTask"},
        ),
        IncidentStatus.RESOLVED,
    )
    with patch("app.api.routes.incidents.incident_store", _store([existing])):
        assert _has_active_incident(_crash_event()) is False


def test_normalized_signature_still_matches_across_different_malformed_values():
    """Same underlying bug, different malformed input value each time (bare
    numbers/IDs are scrubbed from the signature) — must still count as the same
    crash, matching pending_events' own normalization behavior."""
    original = _crash_event('CastError: cast to Number failed for value "8935194"')
    recurrence = _crash_event('CastError: cast to Number failed for value "1234567"')
    existing = _incident_for(original, IncidentStatus.AWAITING_APPROVAL)
    with patch("app.api.routes.incidents.incident_store", _store([existing])):
        assert _has_active_incident(recurrence) is True
