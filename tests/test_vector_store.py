"""
Tests for the vector-store abstraction (`app/services/vector_store.py`).

The ChromaDB backend is exercised against an in-process collection (no
network); the pgvector backend is exercised via mocks because requiring
a live Postgres in CI would mean spinning up a server. Behaviour-only
tests on PgVectorCollection ensure SQL is emitted in the right shape;
end-to-end pgvector validation lives in the live integration test that
runs after PR 5 (RDS) is provisioned.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.services.vector_store import (
    EMBEDDING_DIM,
    ChromaCollection,
    VectorItem,
    VectorMatch,
    _EmptyCollection,
    make_collection,
)


# ---------------------------------------------------------------------------
# ChromaCollection — round-trip against a real in-memory ChromaDB instance
# ---------------------------------------------------------------------------

@pytest.fixture
def chroma_coll(tmp_path) -> ChromaCollection:
    """Fresh ChromaDB rooted in a tmp dir — one collection per test."""
    return ChromaCollection("test_codebase", chroma_path=str(tmp_path / ".chromadb"))


def _vec(value: float = 0.1) -> list[float]:
    return [value] * EMBEDDING_DIM


def test_chroma_count_starts_at_zero(chroma_coll):
    assert chroma_coll.count() == 0


def test_chroma_upsert_round_trip(chroma_coll):
    chroma_coll.upsert([VectorItem(
        id="abc",
        document="def foo(): pass",
        metadata={"chunk_id": "abc", "file_path": "src/a.py"},
        embedding=_vec(0.1),
    )])
    assert chroma_coll.count() == 1

    matches = chroma_coll.query(_vec(0.1), n_results=1)
    assert len(matches) == 1
    assert matches[0].id == "abc"
    assert matches[0].document == "def foo(): pass"
    # Cosine similarity to itself ≈ 1.0
    assert matches[0].score >= 0.99


def test_chroma_query_caps_at_n_results(chroma_coll):
    items = [
        VectorItem(
            id=f"id{i}", document=f"doc {i}",
            metadata={"chunk_id": f"id{i}"}, embedding=_vec(0.1 * i),
        )
        for i in range(5)
    ]
    chroma_coll.upsert(items)
    matches = chroma_coll.query(_vec(0.1), n_results=3)
    assert len(matches) == 3


def test_chroma_get_by_filter_returns_only_matching(chroma_coll):
    chroma_coll.upsert([
        VectorItem(id="a", document="A", metadata={"chunk_id": "a", "file_path": "x.py"}, embedding=_vec()),
        VectorItem(id="b", document="B", metadata={"chunk_id": "b", "file_path": "y.py"}, embedding=_vec()),
    ])
    matches = chroma_coll.get_by_filter({"file_path": "x.py"})
    assert len(matches) == 1
    assert matches[0].id == "a"


def test_chroma_all_metadata_lists_every_row(chroma_coll):
    chroma_coll.upsert([
        VectorItem(id="a", document="A", metadata={"chunk_id": "a", "file_path": "x.py"}, embedding=_vec()),
        VectorItem(id="b", document="B", metadata={"chunk_id": "b", "file_path": "y.py"}, embedding=_vec()),
    ])
    metas = chroma_coll.all_metadata()
    assert len(metas) == 2
    file_paths = sorted(m["file_path"] for m in metas)
    assert file_paths == ["x.py", "y.py"]


def test_chroma_all_items_returns_full_rows(chroma_coll):
    chroma_coll.upsert([
        VectorItem(id="a", document="hello", metadata={"chunk_id": "a"}, embedding=_vec()),
    ])
    items = chroma_coll.all_items()
    assert len(items) == 1
    assert items[0].id == "a"
    assert items[0].document == "hello"


def test_chroma_clear_empties_collection(chroma_coll):
    chroma_coll.upsert([
        VectorItem(id="a", document="A", metadata={"chunk_id": "a"}, embedding=_vec()),
    ])
    assert chroma_coll.count() == 1
    chroma_coll.clear()
    assert chroma_coll.count() == 0


def test_chroma_upsert_replaces_existing(chroma_coll):
    """Same id twice → overwrites, doesn't duplicate."""
    chroma_coll.upsert([
        VectorItem(id="a", document="first", metadata={"chunk_id": "a"}, embedding=_vec(0.1)),
    ])
    chroma_coll.upsert([
        VectorItem(id="a", document="second", metadata={"chunk_id": "a"}, embedding=_vec(0.2)),
    ])
    assert chroma_coll.count() == 1
    matches = chroma_coll.query(_vec(0.2), n_results=1)
    assert matches[0].document == "second"


# ---------------------------------------------------------------------------
# Factory — picks the right backend based on DIALECT
# ---------------------------------------------------------------------------

def test_make_collection_uses_chroma_for_sqlite(tmp_path, monkeypatch):
    """Default dev dialect (sqlite) → ChromaCollection."""
    monkeypatch.setattr("app.services.vector_store.PgVectorCollection", MagicMock())
    coll = make_collection("test_factory_sqlite", chroma_path=str(tmp_path / ".chromadb"))
    assert isinstance(coll, ChromaCollection)


def test_make_collection_falls_back_when_chroma_init_fails(monkeypatch):
    """Path unwritable / chromadb broken → empty stub instead of crash."""
    def boom(*args, **kwargs):
        raise RuntimeError("simulated chroma init failure")
    monkeypatch.setattr("app.services.vector_store.ChromaCollection", boom)
    coll = make_collection("test_factory_failure")
    assert isinstance(coll, _EmptyCollection)
    assert coll.count() == 0


def test_make_collection_uses_pgvector_when_dialect_is_postgres(monkeypatch):
    """Postgres dialect → PgVectorCollection (with init mocked so no real DB)."""
    monkeypatch.setattr("app.services.database.DIALECT", "postgresql")

    fake_pg = MagicMock(spec=[])
    fake_pg_class = MagicMock(return_value=fake_pg)
    monkeypatch.setattr("app.services.vector_store.PgVectorCollection", fake_pg_class)

    coll = make_collection("test_factory_pg")
    assert coll is fake_pg
    fake_pg_class.assert_called_once_with("test_factory_pg")


def test_make_collection_falls_back_when_pgvector_init_fails(monkeypatch):
    """pgvector extension missing → empty stub, NOT a crash."""
    monkeypatch.setattr("app.services.database.DIALECT", "postgresql")

    def boom(*args, **kwargs):
        raise RuntimeError("CREATE EXTENSION vector failed")
    monkeypatch.setattr("app.services.vector_store.PgVectorCollection", boom)

    coll = make_collection("test_factory_pg_failure")
    assert isinstance(coll, _EmptyCollection)
    assert coll.count() == 0


# ---------------------------------------------------------------------------
# _EmptyCollection — degraded-mode contract
# ---------------------------------------------------------------------------

def test_empty_collection_satisfies_protocol():
    coll = _EmptyCollection("noop")
    assert coll.count() == 0
    assert coll.query(_vec(), 5) == []
    assert coll.get_by_filter({}) == []
    assert coll.all_metadata() == []
    assert coll.all_items() == []
    # Mutators must not raise.
    coll.upsert([VectorItem(id="x", document="d", metadata={}, embedding=_vec())])
    coll.clear()
