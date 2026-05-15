"""
Vector-store abstraction over the two backends RAGService can run on:
  - ChromaDB (local-dev default, persistent on disk under .chromadb/)
  - pgvector (production, alongside the existing RDS Postgres database)

The choice is driven by the SQLAlchemy `DIALECT` from app.services.database:
  sqlite      → ChromaCollection
  postgresql  → PgVectorCollection

Both backends expose the same `VectorCollection` surface:
  count() / upsert(items) / query(embedding, n) / get_by_filter() /
  all_metadata() / clear()

Items are passed in as `VectorItem`s (id + document text + metadata + the
1536-dim embedding from text-embedding-3-small). Query results come back
as `VectorMatch`es with cosine similarity in [0.0, 1.0] — *higher is
better* in both backends.

When the backend can't be initialised (e.g. pgvector extension missing on
a fresh RDS instance, or chromadb path unwritable), the service degrades
gracefully — `count()` returns 0 and queries return [] so RAG-dependent
agents fall back to cold-start behaviour rather than crashing.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

# OpenAI text-embedding-3-small dimension. Hardcoded because changing it
# would invalidate every existing embedding anyway.
EMBEDDING_DIM = 1536


@dataclass
class VectorItem:
    """One row to be upserted into a collection."""
    id: str
    document: str
    metadata: dict[str, Any]
    embedding: Sequence[float]


@dataclass
class VectorMatch:
    """One hit returned by a query()."""
    id: str
    document: str
    metadata: dict[str, Any]
    score: float = 0.0   # cosine similarity, higher is better


class VectorCollection(ABC):
    """Backend-agnostic interface RAGService talks to."""

    @abstractmethod
    def count(self) -> int: ...

    @abstractmethod
    def upsert(self, items: Sequence[VectorItem]) -> None: ...

    @abstractmethod
    def query(self, embedding: Sequence[float], n_results: int = 5) -> list[VectorMatch]: ...

    @abstractmethod
    def get_by_filter(self, where: dict[str, Any]) -> list[VectorMatch]: ...

    @abstractmethod
    def all_metadata(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def all_items(self) -> list[VectorMatch]: ...

    @abstractmethod
    def delete(self, ids: Sequence[str]) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...


# ---------------------------------------------------------------------------
# ChromaDB backend — local-dev default
# ---------------------------------------------------------------------------

class ChromaCollection(VectorCollection):
    """Wraps a chromadb.Collection. Persistence lives under .chromadb/."""

    def __init__(self, name: str, chroma_path: str = ".chromadb") -> None:
        import chromadb
        self._name = name
        self._chroma = chromadb.PersistentClient(path=chroma_path)
        self._coll = self._chroma.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    def count(self) -> int:
        try:
            return self._coll.count()
        except Exception as exc:
            logger.debug("[Chroma] count failed: %s", exc)
            return 0

    def upsert(self, items: Sequence[VectorItem]) -> None:
        if not items:
            return
        self._coll.upsert(
            ids=[i.id for i in items],
            documents=[i.document for i in items],
            embeddings=[list(i.embedding) for i in items],
            metadatas=[i.metadata for i in items],
        )

    def query(self, embedding: Sequence[float], n_results: int = 5) -> list[VectorMatch]:
        if self.count() == 0:
            return []
        results = self._coll.query(
            query_embeddings=[list(embedding)],
            n_results=min(n_results, self.count()),
            include=["documents", "metadatas", "distances"],
        )
        out: list[VectorMatch] = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            out.append(VectorMatch(
                id=meta.get("chunk_id") or meta.get("incident_id") or "",
                document=doc,
                metadata=dict(meta),
                score=round(1 - dist, 4),
            ))
        return out

    def get_by_filter(self, where: dict[str, Any]) -> list[VectorMatch]:
        results = self._coll.get(where=where, include=["documents", "metadatas"])
        return [
            VectorMatch(
                id=meta.get("chunk_id") or meta.get("incident_id") or "",
                document=doc,
                metadata=dict(meta),
                score=0.0,
            )
            for doc, meta in zip(results["documents"], results["metadatas"])
        ]

    def all_metadata(self) -> list[dict[str, Any]]:
        if self.count() == 0:
            return []
        results = self._coll.get(include=["metadatas"])
        return [dict(m) for m in results["metadatas"]]

    def all_items(self) -> list[VectorMatch]:
        if self.count() == 0:
            return []
        results = self._coll.get(include=["documents", "metadatas"])
        return [
            VectorMatch(
                id=meta.get("chunk_id") or meta.get("incident_id") or "",
                document=doc,
                metadata=dict(meta),
                score=0.0,
            )
            for doc, meta in zip(results["documents"], results["metadatas"])
        ]

    def delete(self, ids: Sequence[str]) -> None:
        if ids:
            self._coll.delete(ids=list(ids))

    def clear(self) -> None:
        self._chroma.delete_collection(self._name)
        self._coll = self._chroma.get_or_create_collection(
            name=self._name,
            metadata={"hnsw:space": "cosine"},
        )


# ---------------------------------------------------------------------------
# pgvector backend — production Postgres
# ---------------------------------------------------------------------------

class PgVectorCollection(VectorCollection):
    """SQLAlchemy + pgvector implementation.

    Tables are created on first use (after `CREATE EXTENSION vector`). All
    queries use the cosine-distance operator `<=>`; we convert distance to
    similarity with `1 - dist` to match ChromaCollection's [0, 1] range.
    """

    def __init__(self, name: str) -> None:
        from sqlalchemy import (
            Column, MetaData, String, Table, Text, text as _text,
        )
        from sqlalchemy.dialects.postgresql import JSONB
        from pgvector.sqlalchemy import Vector

        from app.services.database import engine

        self._name = name
        self._engine = engine

        # Lazily ensure the extension exists. Idempotent + cheap.
        with engine.begin() as conn:
            conn.execute(_text("CREATE EXTENSION IF NOT EXISTS vector"))

        # Per-collection metadata so name mangling doesn't bleed into
        # the global database schema.
        meta = MetaData()
        self._table = Table(
            f"vec_{name}",
            meta,
            Column("id", String, primary_key=True),
            Column("document", Text, nullable=False),
            Column("meta", JSONB, nullable=False),
            Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        )
        meta.create_all(engine)

    def count(self) -> int:
        from sqlalchemy import func, select
        try:
            with self._engine.connect() as conn:
                return int(conn.execute(select(func.count()).select_from(self._table)).scalar() or 0)
        except Exception as exc:
            logger.debug("[PgVector] count failed for %s: %s", self._name, exc)
            return 0

    def upsert(self, items: Sequence[VectorItem]) -> None:
        if not items:
            return
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        rows = [
            {
                "id": i.id,
                "document": i.document,
                "meta": i.metadata,
                "embedding": list(i.embedding),
            }
            for i in items
        ]
        stmt = pg_insert(self._table).values(rows)
        update_cols = {
            "document": stmt.excluded.document,
            "meta": stmt.excluded.meta,
            "embedding": stmt.excluded.embedding,
        }
        stmt = stmt.on_conflict_do_update(index_elements=["id"], set_=update_cols)
        with self._engine.begin() as conn:
            conn.execute(stmt)

    def query(self, embedding: Sequence[float], n_results: int = 5) -> list[VectorMatch]:
        if self.count() == 0:
            return []
        # Use the cosine distance operator from pgvector. SQLAlchemy doesn't
        # expose it as a method directly on the column for older versions;
        # the literal_column-based approach is portable across pgvector
        # python client versions.
        from sqlalchemy import select

        try:
            distance = self._table.c.embedding.cosine_distance(list(embedding))
        except AttributeError:
            # Older pgvector-python — fall back to a textual operator.
            from sqlalchemy import bindparam
            distance = self._table.c.embedding.op("<=>")(bindparam("q", list(embedding)))

        stmt = (
            select(
                self._table.c.id,
                self._table.c.document,
                self._table.c.meta,
                distance.label("distance"),
            )
            .order_by(distance.asc())
            .limit(n_results)
        )
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [
            VectorMatch(
                id=r.id,
                document=r.document,
                metadata=dict(r.meta or {}),
                score=round(max(0.0, 1.0 - float(r.distance)), 4),
            )
            for r in rows
        ]

    def get_by_filter(self, where: dict[str, Any]) -> list[VectorMatch]:
        from sqlalchemy import select

        # JSONB containment — `meta @> '{...}'`. pgvector docs recommend
        # filter-then-search; for now we handle exact-match-on-keys
        # equivalents to ChromaDB's where filter.
        stmt = select(self._table.c.id, self._table.c.document, self._table.c.meta)
        if where:
            stmt = stmt.where(self._table.c.meta.contains(where))
        with self._engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [
            VectorMatch(id=r.id, document=r.document, metadata=dict(r.meta or {}))
            for r in rows
        ]

    def all_metadata(self) -> list[dict[str, Any]]:
        from sqlalchemy import select
        with self._engine.connect() as conn:
            rows = conn.execute(select(self._table.c.meta)).all()
        return [dict(r.meta or {}) for r in rows]

    def all_items(self) -> list[VectorMatch]:
        from sqlalchemy import select
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(self._table.c.id, self._table.c.document, self._table.c.meta)
            ).all()
        return [
            VectorMatch(id=r.id, document=r.document, metadata=dict(r.meta or {}))
            for r in rows
        ]

    def delete(self, ids: Sequence[str]) -> None:
        if not ids:
            return
        from sqlalchemy import delete as sa_delete
        with self._engine.begin() as conn:
            conn.execute(sa_delete(self._table).where(self._table.c.id.in_(list(ids))))

    def clear(self) -> None:
        with self._engine.begin() as conn:
            conn.execute(self._table.delete())


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_collection(name: str, chroma_path: str = ".chromadb") -> VectorCollection:
    """Pick the right backend based on the SQLAlchemy dialect of the main DB.

    Local dev (sqlite)        → ChromaDB at .chromadb/
    Production (postgresql)   → pgvector table `vec_<name>` in the same DB

    If the chosen backend can't be initialised (e.g. pgvector extension
    missing, or chromadb path unwritable) we fall back to a stub
    collection that always reports empty so the agents degrade gracefully.
    """
    from app.services.database import DIALECT

    if DIALECT == "postgresql":
        try:
            return PgVectorCollection(name)
        except Exception as exc:
            logger.warning("[VectorStore] pgvector init failed for %s — falling back to empty stub: %s", name, exc)
            return _EmptyCollection(name)

    # sqlite (or unknown) → ChromaDB. Any init failure (e.g. read-only fs
    # in a Docker container without a volume mount) also falls back.
    try:
        return ChromaCollection(name, chroma_path=chroma_path)
    except Exception as exc:
        logger.warning("[VectorStore] ChromaDB init failed for %s — falling back to empty stub: %s", name, exc)
        return _EmptyCollection(name)


class _EmptyCollection(VectorCollection):
    """Inert backend used when neither real backend can initialise.

    Lets the platform start up and serve traffic — RAG-dependent agents
    just see an empty index and fall through to cold-start behaviour.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def count(self) -> int: return 0
    def upsert(self, items: Sequence[VectorItem]) -> None: pass
    def query(self, embedding: Sequence[float], n_results: int = 5) -> list[VectorMatch]: return []
    def get_by_filter(self, where: dict[str, Any]) -> list[VectorMatch]: return []
    def all_metadata(self) -> list[dict[str, Any]]: return []
    def all_items(self) -> list[VectorMatch]: return []
    def delete(self, ids: Sequence[str]) -> None: pass
    def clear(self) -> None: pass
