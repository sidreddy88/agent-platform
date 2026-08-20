"""
Regression tests for IncidentStore.get_resolved_for_error()'s description matching.

Real production bug: matching on error_type + service ALONE is far too coarse
for a service that crashes for many unrelated reasons under the same generic
"APP_CRASHED" type. Confirmed live: a previewCode CastError incident's
"regression check" surfaced an unrelated image-upload crash (different route,
different file, different root cause entirely, incident b71aa206 / PR #2279 —
which only ever touched routes/api/image.js) as "the same error, previously
resolved" -- purely because both happened to be APP_CRASHED on
TaskTargetApp. DiagnosisAgent's prompt frames this as "PRIOR KNOWLEDGE --
treat as strong evidence" with no verification at all, and the model produced
a confident, specific-sounding root_cause citing the wrong incident/PR as
evidence that 7 sibling files were already fixed -- a detail that wasn't even
in the prior-knowledge text, blended in from AGENTS.md's own illustrative
anecdote about a similar (but different) incident.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from unittest.mock import patch

from app.models.events import ErrorEvent, EventSource, IncidentState, IncidentStatus
from app.services.incident_store import IncidentStore


@contextmanager
def _no_live_db():
    """get_resolved_for_error always queries the real DB directly (by design,
    for cross-ECS-task correctness) regardless of how the IncidentStore
    instance was built -- there's a real local agent_platform.db SQLite file
    in this dev environment, so an unpatched call would silently query THAT
    instead of the in-memory _incidents this test actually sets up. Force the
    documented "DB unavailable" fallback path (same one production hits
    against an unreachable Postgres) so these tests exercise the in-memory
    matching logic deterministically."""
    with patch("app.services.database.engine.connect", side_effect=RuntimeError("no db in test")):
        yield


def _resolved_incident(
    description: str, diagnosis: str = "some root cause", resolved_at: datetime | None = None,
) -> IncidentState:
    event = ErrorEvent(
        source=EventSource.CLOUDWATCH,
        error_type="APP_CRASHED",
        title="APP_CRASHED in TaskTargetApp",
        service="TaskTargetApp",
        description=description,
    )
    inc = IncidentState(error_event=event)
    inc.status = IncidentStatus.RESOLVED
    inc.diagnosis = diagnosis
    inc.resolved_at = resolved_at or datetime.utcnow()
    return inc


def _store_with(*incidents: IncidentState) -> IncidentStore:
    store = IncidentStore.__new__(IncidentStore)
    store._incidents = {i.id: i for i in incidents}
    store._monitor_pr_map = {}
    return store


def test_matches_when_description_is_similar():
    """The intended happy path: a genuinely recurring crash with near-identical
    description (only variable tokens differ) still matches."""
    past = _resolved_incident(
        'CastError: Cast to Number failed for value "8935194" at path "previewCode"'
    )
    store = _store_with(past)

    with _no_live_db():
        result = store.get_resolved_for_error(
            "APP_CRASHED", "TaskTargetApp",
            'CastError: Cast to Number failed for value "5341614a" at path "previewCode"',
        )

    assert result is not None
    assert result.id == past.id


def test_does_not_match_unrelated_crash_with_same_error_type_and_service():
    """The actual real-world bug: an unrelated image-upload crash must NOT be
    surfaced as 'the same regression' just because both are APP_CRASHED on the
    same service."""
    unrelated = _resolved_incident(
        "Sharp buffer processing failed: Input buffer contains unsupported image format",
        diagnosis="HEIF upload codec issue in image.js",
    )
    store = _store_with(unrelated)

    with _no_live_db():
        result = store.get_resolved_for_error(
            "APP_CRASHED", "TaskTargetApp",
            'CastError: Cast to Number failed for value "5341614a" at path "previewCode"',
        )

    assert result is None


def test_no_match_when_description_omitted():
    """Matches get_open_pr_for_error's behavior: omitting description means no
    result, not a fallback to the old (too coarse) error_type+service-only match."""
    past = _resolved_incident("anything at all")
    store = _store_with(past)

    with _no_live_db():
        result = store.get_resolved_for_error("APP_CRASHED", "TaskTargetApp")

    assert result is None


def test_returns_most_recent_when_multiple_match():
    desc = 'CastError: Cast to Number failed for value "X" at path "previewCode"'
    older = _resolved_incident(desc, resolved_at=datetime.utcnow() - timedelta(days=30))
    newer = _resolved_incident(desc, resolved_at=datetime.utcnow() - timedelta(days=1))
    store = _store_with(older, newer)

    with _no_live_db():
        result = store.get_resolved_for_error("APP_CRASHED", "TaskTargetApp", desc)

    assert result.id == newer.id


def test_no_match_for_different_service():
    past = _resolved_incident('CastError: Cast to Number failed for value "X" at path "previewCode"')
    past.error_event.service = "OtherService"
    store = _store_with(past)

    with _no_live_db():
        result = store.get_resolved_for_error(
            "APP_CRASHED", "TaskTargetApp",
            'CastError: Cast to Number failed for value "X" at path "previewCode"',
        )

    assert result is None
