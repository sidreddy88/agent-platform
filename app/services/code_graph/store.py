"""
Code graph — Postgres persistence layer.

Reads and writes the `code_graph_edges` table defined in app.services.database.
All call graph edges produced by the tree-sitter parser are persisted here so
the in-memory CodeGraph can be rebuilt after a server restart without re-parsing
the entire codebase.

Three operations:
  persist_edges(edges)            — bulk insert after a full or incremental index run
  load_edges()                    — load all rows to rebuild the in-memory graph on startup
  delete_edges_for_file(path)     — remove stale edges before re-indexing a changed file
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select

from app.services.database import engine, tables

logger = logging.getLogger(__name__)

# The table object — shorthand so call sites read cleanly
_t = tables.code_graph_edges


def persist_edges(edges: list[dict[str, Any]]) -> int:
    """Bulk-insert call graph edges into Postgres.

    Each edge dict must have keys: caller_file, caller_function, callee_name, line.
    indexed_at is added automatically.

    Returns the number of rows inserted.
    Does NOT clear existing rows first — call delete_edges_for_file() before
    re-indexing a single file, or clear_all_edges() before a full re-index.
    """
    if not edges:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "caller_file":     e["caller_file"],
            "caller_function": e["caller_function"],
            "callee_name":     e["callee_name"],
            "line":            e["line"],
            "indexed_at":      now,
        }
        for e in edges
    ]

    with engine.begin() as conn:
        conn.execute(_t.insert(), rows)

    logger.debug("[CodeGraph:store] persisted %d edges", len(rows))
    return len(rows)


def load_edges() -> list[dict[str, Any]]:
    """Load all call graph edges from Postgres.

    Used at startup to rebuild the in-memory CodeGraph without re-parsing
    the codebase. Returns a list of dicts with keys:
      caller_file, caller_function, callee_name, line
    """
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                _t.c.caller_file,
                _t.c.caller_function,
                _t.c.callee_name,
                _t.c.line,
            )
        ).fetchall()

    result = [
        {
            "caller_file":     r.caller_file,
            "caller_function": r.caller_function,
            "callee_name":     r.callee_name,
            "line":            r.line,
        }
        for r in rows
    ]
    logger.debug("[CodeGraph:store] loaded %d edges from DB", len(result))
    return result


def delete_edges_for_file(caller_file: str) -> int:
    """Delete all edges where caller_file matches the given path.

    Used for incremental re-indexing: before re-parsing a changed file,
    remove its stale edges so duplicates don't accumulate.

    Returns the number of rows deleted.
    """
    with engine.begin() as conn:
        result = conn.execute(
            delete(_t).where(_t.c.caller_file == caller_file)
        )
    deleted = result.rowcount
    logger.debug("[CodeGraph:store] deleted %d stale edges for %s", deleted, caller_file)
    return deleted


def clear_all_edges() -> int:
    """Delete every row in code_graph_edges. Used before a full re-index."""
    with engine.begin() as conn:
        result = conn.execute(delete(_t))
    deleted = result.rowcount
    logger.debug("[CodeGraph:store] cleared all %d edges", deleted)
    return deleted


def edge_count() -> int:
    """Return the total number of persisted edges. Useful for health checks."""
    from sqlalchemy import func, select as sa_select
    with engine.begin() as conn:
        row = conn.execute(sa_select(func.count()).select_from(_t)).fetchone()
    return row[0] if row else 0
