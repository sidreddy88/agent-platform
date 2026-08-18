"""
Regression tests for POST /incidents/{id}/restart.

Real production bug, found live: the old implementation reset the SAME
incident's fields in place (status -> OPEN, pr_url -> None, etc.) and
re-enqueued its original error_event unchanged. Two compounding bugs made
this never actually work:
  1. Resetting status to OPEN *before* re-enqueueing made the SQL open-PR
     dedup gate (IncidentStore.get_open_pr_for_error) match the incident
     against ITSELF -- the re-enqueued event was silently dropped as a
     duplicate of the very incident being restarted, every time. Confirmed
     live: the incident sat at status=open forever, zero triage/diagnosis
     activity, zero errors, zero traces.
  2. Even without the self-match, IncidentLoop._process() unconditionally
     creates a brand new incident for every event that clears the dedup
     gates -- it never updates an existing incident by ID -- so the reset
     fields would never have been populated by the restarted run anyway.
  3. Nulling incident.pr_url in place without forgetting its monitor_pr_map
     entry first orphans that mapping forever (see
     IncidentStore.forget_pr_mapping's docstring) -- confirmed live: a
     restarted-then-deleted incident's stale PR mapping caused the next
     fresh trigger for the same bug to be wrongly marked "duplicate" of a
     long-closed PR.

The fix: delete the incident and enqueue a fresh copy of its event (the
same pattern delete_incident + POST /trigger already use, manually
verified end-to-end).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.api.routes.incidents import RestartBody, restart_incident
from app.models.events import ErrorEvent, EventSource, IncidentState
from app.services.incident_store import IncidentStore


def _store_with(incident: IncidentState) -> IncidentStore:
    store = IncidentStore.__new__(IncidentStore)
    store._incidents = {incident.id: incident}
    store._monitor_pr_map = {}
    return store


def _incident(pr_url: str | None = None) -> IncidentState:
    event = ErrorEvent(
        source=EventSource.CLOUDWATCH,
        error_type="APP_CRASHED",
        title="APP_CRASHED in TaskAllInterviews",
        service="TaskAllInterviews",
        description="CastError: Cast to Number failed for value \"5341614a\"",
        metadata={"log_group": "/ecs/TaskAllInterviews"},
    )
    inc = IncidentState(error_event=event)
    inc.pr_url = pr_url
    return inc


@pytest.mark.asyncio
async def test_restart_deletes_original_incident():
    incident = _incident()
    store = _store_with(incident)
    with (
        patch("app.api.routes.incidents.incident_store", store),
        patch("app.api.routes.incidents.pending_event_store") as mock_pending,
        patch("app.api.routes.incidents.event_queue") as mock_queue,
        patch("app.api.routes.incidents.broadcast", new=AsyncMock()),
    ):
        mock_queue.enqueue = AsyncMock()
        await restart_incident(incident.id, RestartBody())

    assert store.get(incident.id) is None
    mock_pending.forget_matching.assert_called_once_with(incident.error_event)


@pytest.mark.asyncio
async def test_restart_forgets_stale_pr_mapping_when_pr_url_set():
    """The actual real-world trigger: a restarted incident that had an open PR
    must forget that mapping, or the next fresh trigger for the same bug gets
    wrongly marked duplicate of it."""
    pr_url = "https://github.com/org/repo/pull/2570"
    incident = _incident(pr_url=pr_url)
    store = _store_with(incident)
    store._monitor_pr_map = {incident.monitor_id or "key": pr_url}

    with (
        patch("app.api.routes.incidents.incident_store", store),
        patch("app.api.routes.incidents.pending_event_store"),
        patch("app.api.routes.incidents.event_queue") as mock_queue,
        patch("app.api.routes.incidents.broadcast", new=AsyncMock()),
    ):
        mock_queue.enqueue = AsyncMock()
        await restart_incident(incident.id, RestartBody())

    assert pr_url not in store._monitor_pr_map.values()


@pytest.mark.asyncio
async def test_restart_enqueues_a_fresh_event_not_the_original():
    """A fresh event.id — not the original error_event mutated in place — so
    the SQL dedup gate can't self-match against the (now-deleted) incident."""
    incident = _incident()
    original_event_id = incident.error_event.id
    store = _store_with(incident)

    with (
        patch("app.api.routes.incidents.incident_store", store),
        patch("app.api.routes.incidents.pending_event_store"),
        patch("app.api.routes.incidents.event_queue") as mock_queue,
        patch("app.api.routes.incidents.broadcast", new=AsyncMock()),
    ):
        mock_queue.enqueue = AsyncMock()
        result = await restart_incident(incident.id, RestartBody())

    enqueued_event = mock_queue.enqueue.call_args[0][0]
    assert enqueued_event.id != original_event_id
    assert enqueued_event.description == incident.error_event.description
    assert enqueued_event.metadata["restarted"] is True
    assert enqueued_event.metadata["restarted_from_incident_id"] == incident.id
    assert result["new_event_id"] == enqueued_event.id
    assert result["original_incident_id"] == incident.id


@pytest.mark.asyncio
async def test_restart_notes_carried_via_event_metadata():
    """Notes must survive the delete-and-recreate so FixGenerationAgent's
    human_notes injection still works — see IncidentLoop._process's
    event.metadata["restart_notes"] handling."""
    incident = _incident()
    store = _store_with(incident)

    with (
        patch("app.api.routes.incidents.incident_store", store),
        patch("app.api.routes.incidents.pending_event_store"),
        patch("app.api.routes.incidents.event_queue") as mock_queue,
        patch("app.api.routes.incidents.broadcast", new=AsyncMock()),
    ):
        mock_queue.enqueue = AsyncMock()
        await restart_incident(incident.id, RestartBody(notes="please also handle nulls"))

    enqueued_event = mock_queue.enqueue.call_args[0][0]
    assert enqueued_event.metadata["restart_notes"] == "please also handle nulls"


@pytest.mark.asyncio
async def test_restart_missing_incident_raises_404():
    from fastapi import HTTPException

    store = IncidentStore.__new__(IncidentStore)
    store._incidents = {}
    store._monitor_pr_map = {}

    with patch("app.api.routes.incidents.incident_store", store):
        with pytest.raises(HTTPException) as exc_info:
            await restart_incident("does-not-exist", RestartBody())

    assert exc_info.value.status_code == 404
