"""
Auth tests for /debug/* — every route here requires ADMIN_API_TOKEN
(app/api/auth.py:require_admin_token), applied at the router level.

Not testing the RAG/code-graph behavior itself here — just that the auth
gate actually gates. See test_rag.py / code_graph tests for behavior.
"""
from __future__ import annotations

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes.debug import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TestDebugRouteAuth:
    def test_unset_token_fails_closed_503(self):
        client = _client()
        with patch("app.api.auth.settings.admin_api_token", ""):
            resp = client.get("/debug/rag/corpus")
        assert resp.status_code == 503

    def test_missing_header_returns_401(self):
        client = _client()
        with patch("app.api.auth.settings.admin_api_token", "test-admin-token"):
            resp = client.get("/debug/rag/corpus")
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self):
        client = _client()
        with patch("app.api.auth.settings.admin_api_token", "test-admin-token"):
            resp = client.get("/debug/rag/corpus", headers={"X-Admin-Token": "wrong"})
        assert resp.status_code == 401

    def test_correct_header_token_passes_auth(self):
        client = _client()
        with patch("app.api.auth.settings.admin_api_token", "test-admin-token"):
            with patch("app.services.rag.RAGService") as mock_rag_cls:
                mock_rag_cls.side_effect = Exception("RAG unavailable in test")
                resp = client.get(
                    "/debug/rag/corpus", headers={"X-Admin-Token": "test-admin-token"}
                )
        # Auth passed (not 401/503-from-auth) -- fails downstream instead,
        # on the mocked-out RAGService, which is a different 503.
        assert resp.status_code == 503
        assert "RAG unavailable" in resp.json()["detail"]

    def test_correct_query_param_token_passes_auth(self):
        client = _client()
        with patch("app.api.auth.settings.admin_api_token", "test-admin-token"):
            with patch("app.services.rag.RAGService") as mock_rag_cls:
                mock_rag_cls.side_effect = Exception("RAG unavailable in test")
                resp = client.get("/debug/rag/corpus?token=test-admin-token")
        assert resp.status_code == 503
        assert "RAG unavailable" in resp.json()["detail"]

    def test_gates_the_codebase_corpus_route_specifically(self):
        """The route that actually exposes real indexed target-app content."""
        client = _client()
        with patch("app.api.auth.settings.admin_api_token", "test-admin-token"):
            resp = client.get("/debug/rag/corpus/codebase")
        assert resp.status_code == 401

    def test_gates_post_index_routes_too(self):
        client = _client()
        with patch("app.api.auth.settings.admin_api_token", "test-admin-token"):
            resp = client.post("/debug/rag/index")
        assert resp.status_code == 401
