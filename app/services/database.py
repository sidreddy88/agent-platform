"""
Database layer — SQLAlchemy Core with backend auto-detection.

DATABASE_URL drives the backend:
  unset / empty  → sqlite:///agent_platform.db   (local dev default)
  postgresql://...  → Postgres via psycopg       (production / RDS)
  sqlite:///path     → explicit SQLite

Schema is declared once via SQLAlchemy MetaData; both backends get the
same tables. Application code goes through:

    from app.services.database import engine, tables, upsert
    with engine.begin() as conn:
        conn.execute(insert(tables.incidents).values(...))

`upsert(table, row, conflict_cols)` abstracts the dialect difference
between SQLite's `INSERT OR REPLACE` and Postgres's `INSERT ... ON
CONFLICT (...) DO UPDATE`.

Tables:
  incidents       — full IncidentState JSON + indexed columns
  monitor_pr_map  — resource_id → pr_url dedup map
  approvals       — full ApprovalRequest JSON + indexed columns
  agent_runs      — one row per completed/failed agent run
  monitor_records — MonitorGenerationResult per PR merge
  agent_failures  — per-failure analysis records
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Engine — auto-detect backend from DATABASE_URL
# ---------------------------------------------------------------------------

_DEFAULT_SQLITE_PATH = "agent_platform.db"


def _resolve_url() -> str:
    """Return the SQLAlchemy URL from settings, defaulting to local SQLite."""
    raw = (settings.database_url or "").strip()
    if not raw:
        return f"sqlite:///{_DEFAULT_SQLITE_PATH}"
    # Accept bare `postgres://` (Heroku-style) and translate to the modern
    # SQLAlchemy form so RDS connection strings copy-paste cleanly.
    if raw.startswith("postgres://"):
        raw = "postgresql+psycopg://" + raw.split("://", 1)[1]
    elif raw.startswith("postgresql://") and "+" not in raw.split("://", 1)[0]:
        raw = "postgresql+psycopg://" + raw.split("://", 1)[1]
    return raw


def _create_engine() -> Engine:
    url = _resolve_url()
    is_sqlite = url.startswith("sqlite")
    eng = create_engine(
        url,
        # SQLite needs check_same_thread=False so the engine can be shared
        # across FastAPI's threadpool. Postgres ignores this.
        connect_args={"check_same_thread": False} if is_sqlite else {},
        # Pre-ping checks the connection is alive before handing it out;
        # protects against RDS dropping idle connections.
        pool_pre_ping=not is_sqlite,
        future=True,
    )

    if is_sqlite:
        # WAL + foreign keys mirror the original sqlite3-direct setup.
        @event.listens_for(eng, "connect")
        def _set_sqlite_pragmas(dbapi_conn, _conn_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return eng


engine: Engine = _create_engine()
DIALECT: str = engine.dialect.name  # "sqlite" or "postgresql"


# ---------------------------------------------------------------------------
# Schema (SQLAlchemy MetaData)
# ---------------------------------------------------------------------------

metadata = MetaData()


class _Tables:
    """Namespaced access to all Table objects — `tables.incidents`, etc."""

    incidents = Table(
        "incidents",
        metadata,
        Column("id", String, primary_key=True),
        Column("status", String, nullable=False),
        Column("detected_at", String, nullable=False),
        Column("data", Text, nullable=False),
    )

    monitor_pr_map = Table(
        "monitor_pr_map",
        metadata,
        Column("resource_id", String, primary_key=True),
        Column("pr_url", String, nullable=False),
    )

    approvals = Table(
        "approvals",
        metadata,
        Column("id", String, primary_key=True),
        Column("status", String, nullable=False),
        Column("created_at", String, nullable=False),
        Column("data", Text, nullable=False),
    )

    agent_runs = Table(
        "agent_runs",
        metadata,
        Column("run_id", String, primary_key=True),
        Column("agent_name", String, nullable=False),
        Column("incident_id", String),
        Column("status", String, nullable=False),
        Column("started_at", String, nullable=False),
        Column("completed_at", String),
        Column("duration_ms", Float),
        Column("error_message", Text),
        Column("tool_calls", Integer, default=0),
        Column("input_tokens", Integer, default=0),
        Column("output_tokens", Integer, default=0),
        Column("cost_usd", Float, default=0.0),
        Index("idx_agent_runs_started_at", "started_at"),
        Index("idx_agent_runs_agent_name", "agent_name"),
    )

    # AUTOINCREMENT via SQLAlchemy: BigInteger + autoincrement=True maps to
    # `INTEGER PRIMARY KEY AUTOINCREMENT` (sqlite) and `BIGSERIAL` (postgres).
    monitor_records = Table(
        "monitor_records",
        metadata,
        Column("id", BigInteger, primary_key=True, autoincrement=True),
        Column("repo", String, nullable=False),
        Column("pr_number", Integer, nullable=False),
        Column("generated_at", String, nullable=False),
        Column("data", Text, nullable=False),
    )

    agent_failures = Table(
        "agent_failures",
        metadata,
        Column("id", String, primary_key=True),
        Column("incident_id", String, nullable=False),
        Column("run_id", String),
        Column("agent_name", String, nullable=False),
        Column("failure_category", String, nullable=False),
        Column("failure_reason", Text, nullable=False),
        Column("expected_behavior", Text),
        Column("actual_behavior", Text),
        Column("error_description", Text),
        Column("created_at", String, nullable=False),
        Index("idx_agent_failures_incident_id", "incident_id"),
        Index("idx_agent_failures_agent_name", "agent_name"),
        Index("idx_agent_failures_created_at", "created_at"),
    )

    # Maps each indexed file (doc_id = relative file path) to the chunk vector
    # IDs currently live in the vector store, plus a content hash for change
    # detection. When a file is re-indexed, old chunk IDs are deleted from the
    # vector store and this table is updated atomically.
    doc_chunk_registry = Table(
        "doc_chunk_registry",
        metadata,
        Column("doc_id", String, nullable=False),           # relative file path
        Column("chunk_vector_id", String, nullable=False),  # chunk_id in vector store
        Column("content_hash", String, nullable=False),     # sha256 of file content
        Column("collection", String, nullable=False),       # vector collection name
        Column("indexed_at", String, nullable=False),
        Column("status", String, nullable=False, default="active"),  # active | superseded
        Index("idx_dcr_doc_id", "doc_id"),
        Index("idx_dcr_chunk_vector_id", "chunk_vector_id"),
    )


tables = _Tables()


# ---------------------------------------------------------------------------
# init_db — idempotent schema bootstrap
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create all tables if they don't exist. Safe to call repeatedly."""
    metadata.create_all(engine)
    logger.debug("[DB] Schema initialised on %s", engine.url.render_as_string(hide_password=True))


# ---------------------------------------------------------------------------
# upsert — dialect-portable INSERT ... ON CONFLICT DO UPDATE
# ---------------------------------------------------------------------------

def upsert(
    table: Table,
    row: Mapping[str, Any],
    conflict_cols: list[str] | None = None,
) -> None:
    """Insert `row` into `table`, replacing on conflict.

    `conflict_cols` defaults to the primary key columns of `table`. Both
    SQLite and Postgres support `ON CONFLICT (...) DO UPDATE` via dialect-
    specific `insert()` helpers; we route to the right one based on
    `engine.dialect.name`.
    """
    if conflict_cols is None:
        conflict_cols = [c.name for c in table.primary_key.columns]

    if DIALECT == "sqlite":
        stmt = sqlite_insert(table).values(**row)
        # All non-conflict columns get updated to the new values.
        update_cols = {c: stmt.excluded[c] for c in row.keys() if c not in conflict_cols}
        stmt = stmt.on_conflict_do_update(index_elements=conflict_cols, set_=update_cols)
    elif DIALECT == "postgresql":
        stmt = pg_insert(table).values(**row)
        update_cols = {c: stmt.excluded[c] for c in row.keys() if c not in conflict_cols}
        stmt = stmt.on_conflict_do_update(index_elements=conflict_cols, set_=update_cols)
    else:
        # Unknown dialect — fallback to delete+insert in the same transaction.
        # This path isn't expected in production but keeps the helper safe.
        with engine.begin() as conn:
            pk = list(table.primary_key.columns)[0]
            conn.execute(table.delete().where(pk == row[pk.name]))
            conn.execute(table.insert().values(**row))
        return

    with engine.begin() as conn:
        conn.execute(stmt)


# ---------------------------------------------------------------------------
# Backwards-compat — legacy get_db() shim
# ---------------------------------------------------------------------------
#
# Pre-existing call sites use the sqlite3 connection pattern:
#   conn = get_db()
#   try: rows = conn.execute("SELECT ... WHERE x = ?", (val,)); ...
#   finally: conn.close()
#
# Each call site is being migrated to the SQLAlchemy `engine` API. Until
# the migration is complete, `get_db()` returns the underlying DB-API
# connection for SQLite, which preserves the exact previous behaviour.
# Postgres callers must use the new `engine` / `connect()` API.

def get_db():
    """Legacy shim — returns the underlying sqlite3 connection.

    Raises if the configured backend isn't SQLite. Migration target: every
    caller switches to `engine` + `text()` / table objects.
    """
    if DIALECT != "sqlite":
        raise RuntimeError(
            "get_db() is SQLite-only. Caller must migrate to the SQLAlchemy "
            "`engine` API (from app.services.database import engine, tables)."
        )
    import sqlite3
    conn = sqlite3.connect(_DEFAULT_SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def connect() -> Connection:
    """Convenience for short read-only queries via SQLAlchemy.

    Usage:
        with database.connect() as conn:
            rows = conn.execute(text("SELECT ...")).all()

    Writes should use `with engine.begin() as conn:` so the transaction
    commits on success / rolls back on exception.
    """
    return engine.connect()


# Ensure tables exist whenever this module is imported.
init_db()
