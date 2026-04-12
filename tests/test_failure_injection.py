"""
Tests for FailureInjector and the injection API routes.

Run:
    pytest tests/test_failure_injection.py -v
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.services.failure_injection import FailureInjector, FailureScenario, failure_injector


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_injector():
    """Return a FailureInjector whose event_queue is mocked out."""
    injector = FailureInjector()
    return injector


# ---------------------------------------------------------------------------
# FailureScenario enum
# ---------------------------------------------------------------------------

class TestFailureScenario:
    def test_all_three_scenarios_exist(self):
        scenarios = {s.value for s in FailureScenario}
        assert "false_positive" in scenarios
        assert "duplicate_alert" in scenarios
        assert "cascading_failure" in scenarios


# ---------------------------------------------------------------------------
# false_positive scenario
# ---------------------------------------------------------------------------

class TestFalsePositiveScenario:
    @pytest.mark.asyncio
    async def test_returns_correct_scenario_name(self):
        injector = _make_injector()
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock()
            result = await injector.inject(FailureScenario.FALSE_POSITIVE)
        assert result["scenario"] == FailureScenario.FALSE_POSITIVE

    @pytest.mark.asyncio
    async def test_enqueues_one_event(self):
        injector = _make_injector()
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock()
            result = await injector.inject(FailureScenario.FALSE_POSITIVE)
        assert result["events_injected"] == 1
        assert mock_q.enqueue.call_count == 1

    @pytest.mark.asyncio
    async def test_event_is_health_check_timeout(self):
        injector = _make_injector()
        enqueued = []
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock(side_effect=lambda ev: enqueued.append(ev))
            await injector.inject(FailureScenario.FALSE_POSITIVE)
        assert enqueued[0].error_type == "HEALTH_CHECK_TIMEOUT"

    @pytest.mark.asyncio
    async def test_service_override_applied(self):
        injector = _make_injector()
        enqueued = []
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock(side_effect=lambda ev: enqueued.append(ev))
            await injector.inject(FailureScenario.FALSE_POSITIVE, service="custom-svc")
        assert enqueued[0].service == "custom-svc"

    @pytest.mark.asyncio
    async def test_description_mentions_noise(self):
        injector = _make_injector()
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock()
            result = await injector.inject(FailureScenario.FALSE_POSITIVE)
        assert "noise" in result["description"].lower()


# ---------------------------------------------------------------------------
# duplicate_alert scenario
# ---------------------------------------------------------------------------

class TestDuplicateAlertScenario:
    @pytest.mark.asyncio
    async def test_enqueues_two_events(self):
        injector = _make_injector()
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock()
            with patch("app.services.failure_injection.asyncio.sleep", new=AsyncMock()):
                result = await injector.inject(FailureScenario.DUPLICATE_ALERT)
        assert result["events_injected"] == 2
        assert mock_q.enqueue.call_count == 2

    @pytest.mark.asyncio
    async def test_both_events_have_same_error_type(self):
        injector = _make_injector()
        enqueued = []
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock(side_effect=lambda ev: enqueued.append(ev))
            with patch("app.services.failure_injection.asyncio.sleep", new=AsyncMock()):
                await injector.inject(FailureScenario.DUPLICATE_ALERT)
        assert enqueued[0].error_type == enqueued[1].error_type

    @pytest.mark.asyncio
    async def test_both_events_have_different_ids(self):
        injector = _make_injector()
        enqueued = []
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock(side_effect=lambda ev: enqueued.append(ev))
            with patch("app.services.failure_injection.asyncio.sleep", new=AsyncMock()):
                await injector.inject(FailureScenario.DUPLICATE_ALERT)
        assert enqueued[0].id != enqueued[1].id

    @pytest.mark.asyncio
    async def test_description_mentions_dedup(self):
        injector = _make_injector()
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock()
            with patch("app.services.failure_injection.asyncio.sleep", new=AsyncMock()):
                result = await injector.inject(FailureScenario.DUPLICATE_ALERT)
        assert "dedup" in result["description"].lower()


# ---------------------------------------------------------------------------
# cascading_failure scenario
# ---------------------------------------------------------------------------

class TestCascadingFailureScenario:
    @pytest.mark.asyncio
    async def test_enqueues_four_events(self):
        injector = _make_injector()
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock()
            with patch("app.services.failure_injection.asyncio.sleep", new=AsyncMock()):
                result = await injector.inject(FailureScenario.CASCADING_FAILURE)
        assert result["events_injected"] == 4
        assert mock_q.enqueue.call_count == 4

    @pytest.mark.asyncio
    async def test_events_span_multiple_services(self):
        injector = _make_injector()
        enqueued = []
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock(side_effect=lambda ev: enqueued.append(ev))
            with patch("app.services.failure_injection.asyncio.sleep", new=AsyncMock()):
                await injector.inject(FailureScenario.CASCADING_FAILURE)
        services = {ev.service for ev in enqueued}
        assert len(services) == 4   # each event targets a different service

    @pytest.mark.asyncio
    async def test_all_event_ids_returned(self):
        injector = _make_injector()
        enqueued = []
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock(side_effect=lambda ev: enqueued.append(ev))
            with patch("app.services.failure_injection.asyncio.sleep", new=AsyncMock()):
                result = await injector.inject(FailureScenario.CASCADING_FAILURE)
        expected_ids = {ev.id for ev in enqueued}
        assert set(result["event_ids"]) == expected_ids


# ---------------------------------------------------------------------------
# String-based scenario input
# ---------------------------------------------------------------------------

class TestStringInput:
    @pytest.mark.asyncio
    async def test_accepts_string_scenario(self):
        injector = _make_injector()
        with patch("app.services.failure_injection.event_queue") as mock_q:
            mock_q.enqueue = AsyncMock()
            result = await injector.inject("false_positive")
        assert result["events_injected"] == 1

    @pytest.mark.asyncio
    async def test_unknown_scenario_raises_value_error(self):
        injector = _make_injector()
        with pytest.raises(ValueError):
            await injector.inject("not_a_scenario")


# ---------------------------------------------------------------------------
# Injection API routes
# ---------------------------------------------------------------------------

class TestInjectionRoutes:
    def test_list_scenarios_returns_all_three(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.routes.injection import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.get("/injection/scenarios")
        assert resp.status_code == 200
        scenarios = {s["scenario"] for s in resp.json()}
        assert "false_positive" in scenarios
        assert "duplicate_alert" in scenarios
        assert "cascading_failure" in scenarios

    def test_trigger_false_positive(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.routes.injection import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        with patch("app.api.routes.injection.failure_injector") as mock_fi:
            mock_fi.inject = AsyncMock(return_value={
                "scenario": "false_positive",
                "events_injected": 1,
                "event_ids": ["ev_abc"],
                "description": "test",
            })
            resp = client.post("/injection/trigger", json={"scenario": "false_positive"})

        assert resp.status_code == 200
        assert resp.json()["events_injected"] == 1

    def test_trigger_unknown_scenario_returns_422(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.routes.injection import router

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        resp = client.post("/injection/trigger", json={"scenario": "explode_everything"})
        assert resp.status_code == 422   # Pydantic enum validation


# ---------------------------------------------------------------------------
# Circuit breaker routes
# ---------------------------------------------------------------------------

class TestCircuitBreakerRoutes:
    def test_list_breakers_empty(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.routes.circuit_breaker import router
        from app.services.circuit_breaker import CircuitBreakerRegistry

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        with patch("app.api.routes.circuit_breaker.circuit_breaker_registry",
                   CircuitBreakerRegistry()):
            resp = client.get("/circuit-breakers")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_reset_unknown_returns_404(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.routes.circuit_breaker import router
        from app.services.circuit_breaker import CircuitBreakerRegistry

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        with patch("app.api.routes.circuit_breaker.circuit_breaker_registry",
                   CircuitBreakerRegistry()):
            resp = client.post("/circuit-breakers/nonexistent/reset")
        assert resp.status_code == 404

    def test_reset_known_returns_200(self):
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.routes.circuit_breaker import router
        from app.services.circuit_breaker import CircuitBreakerRegistry

        reg = CircuitBreakerRegistry()
        cb = reg.get_or_create("my_svc")
        cb._state = cb._state.__class__.OPEN   # force open

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        with patch("app.api.routes.circuit_breaker.circuit_breaker_registry", reg):
            resp = client.post("/circuit-breakers/my_svc/reset")
        assert resp.status_code == 200
        assert cb.state.value == "closed"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_is_instance(self):
        assert isinstance(failure_injector, FailureInjector)
