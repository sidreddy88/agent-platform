"""
Tests for the Atlas Performance Advisor pull + the /performance/heaviest
route. No live Atlas calls — `_get` is monkeypatched to return canned
fixture data.
"""
from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.performance import router as performance_router
from app.services import mongodb_atlas as atlas_module
from app.services.mongodb_atlas import (
    MongoDBAtlasService,
    SlowQuery,
    SuggestedIndex,
)


# ---------------------------------------------------------------------------
# Fixtures — Atlas API responses
# ---------------------------------------------------------------------------

PROCESSES_FIXTURE = {
    "results": [
        {"id": "shard-00-00.example.mongodb.net:27017", "typeName": "REPLICA_PRIMARY"},
        {"id": "shard-00-01.example.mongodb.net:27017", "typeName": "REPLICA_SECONDARY"},
    ]
}

SLOW_QUERY_FIXTURE = {
    "slowQueries": [
        {
            "namespace": "interviews.users",
            "line": "{ find: \"users\", filter: { email: 1 } }",
            "opTime": "2026-05-08T01:00:00Z",
            "metrics": {"execCount": 412, "execTimeMillis": 38.0, "totalTimeMillis": 15656.0},
        },
        {
            "namespace": "interviews.posts",
            "line": "{ aggregate: \"posts\", pipeline: [{$match: ...}] }",
            "opTime": "2026-05-08T01:05:00Z",
            "metrics": {"execCount": 50, "execTimeMillis": 220.5, "totalTimeMillis": 11025.0},
        },
    ]
}

SUGGESTED_INDEX_FIXTURE = {
    "suggestedIndexes": [
        {
            "namespace": "interviews.users",
            "index": [{"email": 1}],
            "weight": 95.5,
            "impact": [{"queryShape": "{ find: \"users\", filter: { email: 1 } }"}],
        },
        {
            "namespace": "interviews.posts",
            "index": [{"author": 1}, {"createdAt": -1}],
            "weight": 78.0,
            "impact": [],
        },
    ]
}


@pytest.fixture
def configured_service(monkeypatch) -> MongoDBAtlasService:
    """Atlas service with stubbed credentials and stubbed _get."""
    svc = MongoDBAtlasService()
    monkeypatch.setattr(svc, "_public_key", "stub-public", raising=False)
    monkeypatch.setattr(svc, "_private_key", "stub-private", raising=False)
    monkeypatch.setattr(svc, "_project_id", "abcdef", raising=False)

    async def fake_get(path: str, params: dict | None = None) -> dict[str, Any]:
        if path.endswith("/processes"):
            return PROCESSES_FIXTURE
        if path.endswith("/slowQueryLogs"):
            return SLOW_QUERY_FIXTURE
        if path.endswith("/suggestedIndexes"):
            return SUGGESTED_INDEX_FIXTURE
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(svc, "_get", fake_get)
    return svc


# ---------------------------------------------------------------------------
# Atlas service tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_slow_queries_parses_payload(configured_service: MongoDBAtlasService):
    queries = await configured_service.get_slow_queries("shard-00-00.example.mongodb.net:27017")
    assert len(queries) == 2
    assert queries[0].namespace == "interviews.users"
    assert queries[0].exec_count == 412
    assert queries[0].avg_ms == 38.0
    assert queries[0].total_ms == 15656.0


@pytest.mark.asyncio
async def test_get_suggested_indexes_renders_index_def(configured_service: MongoDBAtlasService):
    idxs = await configured_service.get_suggested_indexes("shard-00-00.example.mongodb.net:27017")
    assert len(idxs) == 2
    # Single-field index renders as "{ email: 1 }".
    assert idxs[0].index_def == "{ email: 1 }"
    # Multi-field index preserves order.
    assert idxs[1].index_def == "{ author: 1, createdAt: -1 }"
    assert idxs[0].weight == 95.5
    assert "email" in idxs[0].impact[0]


@pytest.mark.asyncio
async def test_get_performance_advisor_dedupes_across_processes(
    monkeypatch, configured_service: MongoDBAtlasService,
):
    """Atlas reports the same patterns on every replica-set member; we should dedupe."""
    # Force 2 primary processes so the same fixture data is returned twice.
    multi_process_fixture = {
        "results": [
            {"id": "p1:27017", "typeName": "REPLICA_PRIMARY"},
            {"id": "p2:27017", "typeName": "REPLICA_PRIMARY"},
        ]
    }

    async def fake_get(path: str, params: dict | None = None):
        if path.endswith("/processes"):
            return multi_process_fixture
        if path.endswith("/slowQueryLogs"):
            return SLOW_QUERY_FIXTURE
        if path.endswith("/suggestedIndexes"):
            return SUGGESTED_INDEX_FIXTURE
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr(configured_service, "_get", fake_get)

    slow, idx = await configured_service.get_performance_advisor(hours=24)
    # Two distinct namespaces in the fixture — dedupe should leave 2, not 4.
    assert len(slow) == 2
    assert len(idx) == 2
    # Sorted by total_ms desc — interviews.users has total_ms=15656 > posts 11025.
    assert slow[0].namespace == "interviews.users"


@pytest.mark.asyncio
async def test_unconfigured_service_returns_empty():
    """No credentials → empty results, no exceptions."""
    svc = MongoDBAtlasService()
    # Make sure it's not configured even if the .env has values.
    object.__setattr__(svc, "_public_key", "")
    object.__setattr__(svc, "_private_key", "")
    object.__setattr__(svc, "_project_id", "")

    slow, idx = await svc.get_performance_advisor()
    assert slow == [] and idx == []


@pytest.mark.asyncio
async def test_advisor_swallows_atlas_failures(monkeypatch, configured_service: MongoDBAtlasService):
    """A 500 from Atlas should not propagate as an exception — return empty."""
    async def boom(path: str, params: dict | None = None):
        if path.endswith("/processes"):
            return PROCESSES_FIXTURE
        raise RuntimeError("simulated Atlas 500")

    monkeypatch.setattr(configured_service, "_get", boom)

    slow, idx = await configured_service.get_performance_advisor()
    assert slow == [] and idx == []


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------

@pytest.fixture
def route_client(monkeypatch) -> TestClient:
    """Patch the module-level atlas_service with stubbed payloads."""
    fake_slow = [
        SlowQuery(
            namespace="interviews.users",
            query_shape="{ find: \"users\", filter: { email: 1 } }",
            exec_count=412,
            avg_ms=38.0,
            total_ms=15656.0,
            latest_at="2026-05-08T01:00:00Z",
        ),
    ]
    fake_idx = [
        SuggestedIndex(
            namespace="interviews.users",
            index_def="{ email: 1 }",
            impact=["{ find: \"users\", filter: { email: 1 } }"],
            weight=95.5,
        ),
    ]

    async def fake_advisor(hours: int = 24):
        return fake_slow, fake_idx

    monkeypatch.setattr(atlas_module.atlas_service, "get_performance_advisor", fake_advisor)

    # Reset the route's in-memory cache between tests.
    from app.api.routes import performance as perf_module
    perf_module._cache["payload"] = None
    perf_module._cache["fetched_at_monotonic"] = 0.0

    app = FastAPI()
    app.include_router(performance_router)
    return TestClient(app)


def test_heaviest_returns_advisor_payload(route_client: TestClient):
    resp = route_client.get("/performance/heaviest")
    assert resp.status_code == 200
    data = resp.json()
    assert "slow_queries" in data
    assert "suggested_indexes" in data
    assert "fetched_at" in data
    assert data["slow_queries"][0]["namespace"] == "interviews.users"
    assert data["suggested_indexes"][0]["index_def"] == "{ email: 1 }"


def test_heaviest_clamps_hours_param(route_client: TestClient):
    resp = route_client.get("/performance/heaviest?hours=999")
    assert resp.status_code == 200
    # 999 should clamp to 168 (max).
    assert resp.json()["hours"] == 168


def test_heaviest_caches_within_ttl(route_client: TestClient, monkeypatch):
    """Two requests in quick succession should hit the cache."""
    call_count = {"n": 0}

    async def counting_advisor(hours: int = 24):
        call_count["n"] += 1
        return [], []

    monkeypatch.setattr(atlas_module.atlas_service, "get_performance_advisor", counting_advisor)

    # Reset cache.
    from app.api.routes import performance as perf_module
    perf_module._cache["payload"] = None
    perf_module._cache["fetched_at_monotonic"] = 0.0

    route_client.get("/performance/heaviest")
    route_client.get("/performance/heaviest")
    assert call_count["n"] == 1


def test_heaviest_refresh_bypasses_cache(route_client: TestClient, monkeypatch):
    call_count = {"n": 0}

    async def counting_advisor(hours: int = 24):
        call_count["n"] += 1
        return [], []

    monkeypatch.setattr(atlas_module.atlas_service, "get_performance_advisor", counting_advisor)

    from app.api.routes import performance as perf_module
    perf_module._cache["payload"] = None
    perf_module._cache["fetched_at_monotonic"] = 0.0

    route_client.get("/performance/heaviest")
    route_client.get("/performance/heaviest?refresh=true")
    assert call_count["n"] == 2
