"""Auto-triage: detection triages each new pending event in the background; approval and diagnosis are unchanged."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.triage import TriageResult
from app.models.events import ErrorEvent, EventSource
from app.services import auto_triage as auto_triage_mod
from app.services import detection as detection_mod
from app.services.auto_triage import AutoTriage
from app.services.pending_events import PendingEventStore


def _event(text="TypeError: Cannot read properties of undefined (reading 'id')", category="error") -> ErrorEvent:
    return ErrorEvent(source=EventSource.CLOUDWATCH, error_type="TYPEERROR", title="TYPEERROR in api",
                      description=text, service="api", category=category, metadata={"log_group": "/ecs/api"})


@pytest.fixture
def store(monkeypatch):
    s = PendingEventStore()
    monkeypatch.setattr(detection_mod, "pending_event_store", s)
    monkeypatch.setattr("app.services.pending_events.pending_event_store", s)
    monkeypatch.setattr(detection_mod, "broadcast", AsyncMock())
    monkeypatch.setattr("app.api.websocket_dashboard.broadcast", AsyncMock())
    monkeypatch.setattr(detection_mod.event_queue, "enqueue", AsyncMock())
    return s


def _detection(monkeypatch, events: list[ErrorEvent]):
    svc = detection_mod.DetectionService.__new__(detection_mod.DetectionService)
    for name in ("_detect_ecs", "_detect_ecs_task_clusters", "_detect_ec2", "_detect_cloudwatch_log_filters"):
        monkeypatch.setattr(svc, name, AsyncMock(return_value=[]))
    monkeypatch.setattr(svc, "_detect_ecs_log_errors", AsyncMock(return_value=events))
    return svc


@pytest.fixture
def scheduled(monkeypatch):
    calls: list = []
    monkeypatch.setattr(auto_triage_mod.auto_triage, "schedule", lambda pe: calls.append(pe.id))
    monkeypatch.setattr(detection_mod.settings, "auto_triage_on_detection", True)
    return calls


def test_a_new_pending_event_is_triaged_once(monkeypatch, store, scheduled):
    ev = _event()
    asyncio.run(_detection(monkeypatch, [ev]).poll_once())
    assert scheduled == [ev.id]
    # the same error again merges into the pending event: no second triage
    asyncio.run(_detection(monkeypatch, [_event()]).poll_once())
    assert scheduled == [ev.id]
    assert store.get(ev.id).occurrences == 2


def test_crashes_keep_their_direct_path(monkeypatch, store, scheduled):
    asyncio.run(_detection(monkeypatch, [_event(category="crash")]).poll_once())
    assert scheduled == []
    detection_mod.event_queue.enqueue.assert_awaited_once()


def test_the_setting_turns_it_off(monkeypatch, store, scheduled):
    monkeypatch.setattr(detection_mod.settings, "auto_triage_on_detection", False)
    asyncio.run(_detection(monkeypatch, [_event()]).poll_once())
    assert scheduled == []


def _result(**kw) -> TriageResult:
    base = dict(decision="real", severity="P1", blast_radius="single_service", occurrences_24h=140,
                duplicate_pr=None, reasoning="frequent")
    return TriageResult(**{**base, **kw})


def test_the_result_is_stored_on_the_pending_event_and_served(monkeypatch, store):
    pe, _ = store.add(_event())
    at = AutoTriage()
    at._agent = MagicMock(triage=AsyncMock(return_value=_result()))
    asyncio.run(at.run(pe))
    assert pe.triage["decision"] == "real" and pe.triage["severity"] == "P1"
    assert pe.triage["occurrences_24h"] == 140 and pe.triage["triaged_at"]
    assert store.serialize(pe)["triage"]["reasoning"] == "frequent"


def test_an_event_approved_or_dismissed_meanwhile_is_not_triaged(monkeypatch, store):
    pe, _ = store.add(_event())
    store.remove(pe.id)
    at = AutoTriage()
    at._agent = MagicMock(triage=AsyncMock(return_value=_result()))
    asyncio.run(at.run(pe))
    at._agent.triage.assert_not_awaited()
    assert pe.triage == {}


def test_a_triage_failure_leaves_the_event_untriaged(monkeypatch, store):
    pe, _ = store.add(_event())
    at = AutoTriage()
    at._agent = MagicMock(triage=AsyncMock(side_effect=RuntimeError("API down")))
    asyncio.run(at.run(pe))
    assert pe.triage == {}
    assert store.get(pe.id) is pe                      # still pending, still approvable


def test_untriaged_events_serialize_with_an_empty_triage():
    s = PendingEventStore()
    pe, _ = s.add(_event())
    assert s.serialize(pe)["triage"] == {}
