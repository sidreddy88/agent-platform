"""
Tests for scripts/migrate_to_postgres.py.

Live Postgres isn't available in CI, so the writer is exercised by
pointing `--target` at a *second* SQLite file via `sqlite:///`. The
migration code uses the same SQLAlchemy upsert path for both backends
(only the dialect-specific INSERT statement differs), so this catches
schema-, parameter-, and round-trip bugs without requiring Postgres.
The pgvector path is unit-tested separately in test_vector_store.py.
"""
from __future__ import annotations

import importlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

# Add scripts/ to sys.path so the script's helpers are importable directly.
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import migrate_to_postgres as mig  # noqa: E402


# ---------------------------------------------------------------------------
# _read_sqlite_tables — handles missing / partial source files
# ---------------------------------------------------------------------------

def test_read_sqlite_tables_missing_file_returns_empty(tmp_path):
    out = mig._read_sqlite_tables(tmp_path / "does-not-exist.db")
    assert all(rows == [] for rows in out.values())
    assert set(out.keys()) == set(mig.SQL_TABLES)


def _create_source_db(path: Path, *, with_rows: bool = True) -> None:
    """Build a tiny source SQLite db with the platform's schema."""
    # Reuse the application's metadata so the test schema can't drift.
    import os
    os.environ["DATABASE_URL"] = f"sqlite:///{path}"
    from importlib import reload
    from app.core import config as config_module
    reload(config_module)
    from app.services import database as db_module
    reload(db_module)
    db_module.init_db()

    if with_rows:
        db_module.upsert(db_module.tables.incidents, {
            "id": "inc-1", "status": "resolved",
            "detected_at": "2026-05-09T00:00:00Z",
            "data": '{"id": "inc-1"}',
        })
        db_module.upsert(db_module.tables.approvals, {
            "id": "ap-1", "status": "approved",
            "created_at": "2026-05-09T00:00:00Z",
            "data": '{"id": "ap-1"}',
        })


def test_read_sqlite_tables_returns_rows(tmp_path):
    db_path = tmp_path / "source.db"
    _create_source_db(db_path, with_rows=True)

    out = mig._read_sqlite_tables(db_path)
    assert len(out["incidents"]) == 1
    assert out["incidents"][0]["id"] == "inc-1"
    assert len(out["approvals"]) == 1
    assert out["approvals"][0]["id"] == "ap-1"
    # Tables that were created but empty come back empty, not absent.
    assert out["agent_runs"] == []


def test_read_sqlite_tables_skips_missing_table(tmp_path):
    """An older snapshot might not have every table — those return empty, not raise."""
    db_path = tmp_path / "partial.db"
    conn = sqlite3.connect(str(db_path))
    # Only create one table — the others should come back empty.
    conn.execute("CREATE TABLE incidents (id TEXT PRIMARY KEY, status TEXT, detected_at TEXT, data TEXT)")
    conn.commit()
    conn.close()

    out = mig._read_sqlite_tables(db_path)
    assert out["incidents"] == []
    # All other tables also empty rather than raising:
    for t in mig.SQL_TABLES:
        if t != "incidents":
            assert out[t] == [], f"{t} should be empty"


# ---------------------------------------------------------------------------
# _read_chroma_collections — graceful handling of missing dir
# ---------------------------------------------------------------------------

def test_read_chroma_collections_missing_dir_returns_empty(tmp_path):
    out = mig._read_chroma_collections(tmp_path / ".does-not-exist")
    assert set(out.keys()) == set(mig.VECTOR_COLLECTIONS)
    assert all(items == [] for items in out.values())


# ---------------------------------------------------------------------------
# _verify_target — sanity checks on the URL shape
# ---------------------------------------------------------------------------

def test_verify_target_rejects_non_postgres():
    ok, msg = mig._verify_target("sqlite:///foo.db")
    assert ok is False
    assert "must be Postgres" in msg


def test_verify_target_rejects_unreachable():
    """A clearly-unreachable host fails fast with a connection error."""
    ok, msg = mig._verify_target("postgresql+psycopg://x:y@127.0.0.1:1/none")
    assert ok is False
    # Either the connection attempt fails, or pgvector creation fails — both acceptable.
    assert "could not connect" in msg or "pgvector" in msg


# ---------------------------------------------------------------------------
# End-to-end: write SQL tables to a *second* SQLite file
# ---------------------------------------------------------------------------
# The writer uses SQLAlchemy + dialect-aware upsert; pointing it at a
# sqlite:// target exercises the same code path without needing Postgres.
# This catches schema mismatches, missing columns, parameter-style bugs.

def test_write_sql_tables_round_trip(tmp_path, monkeypatch):
    src = tmp_path / "source.db"
    _create_source_db(src, with_rows=True)

    tables = mig._read_sqlite_tables(src)

    # Force the writer to rebuild against a fresh sqlite:// target.
    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    written = mig._write_sql_tables(target_url, tables)

    assert written["incidents"] == 1
    assert written["approvals"] == 1

    # Verify by reading the target back through the same module
    # (force the module to re-bind to the target URL).
    monkeypatch.setenv("DATABASE_URL", target_url)
    from app.core import config as config_module
    importlib.reload(config_module)
    from app.services import database as db_module
    importlib.reload(db_module)

    from sqlalchemy import select
    with db_module.engine.connect() as conn:
        rows = conn.execute(select(db_module.tables.incidents)).all()
    assert len(rows) == 1
    assert rows[0].id == "inc-1"


def test_write_sql_tables_idempotent(tmp_path):
    """Re-running the writer must not duplicate or fail."""
    src = tmp_path / "source.db"
    _create_source_db(src, with_rows=True)
    tables = mig._read_sqlite_tables(src)

    target_url = f"sqlite:///{tmp_path / 'target.db'}"
    mig._write_sql_tables(target_url, tables)
    mig._write_sql_tables(target_url, tables)

    # Read back — still exactly 1 row.
    import os
    os.environ["DATABASE_URL"] = target_url
    from importlib import reload
    from app.core import config as config_module
    reload(config_module)
    from app.services import database as db_module
    reload(db_module)
    from sqlalchemy import select, func
    with db_module.engine.connect() as conn:
        count = conn.execute(select(func.count()).select_from(db_module.tables.incidents)).scalar()
    assert count == 1


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

def test_cli_dry_run_reports_counts(tmp_path):
    """Running the script with --dry-run should print row counts and exit 0."""
    src = tmp_path / "source.db"
    _create_source_db(src, with_rows=True)

    result = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "migrate_to_postgres.py"),
            "--dry-run",
            "--target", "postgresql+psycopg://stub@localhost:5432/stub",
            "--sqlite", str(src),
            "--chromadb", str(tmp_path / "no-chroma"),
            "--skip-vectors",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "Reading SQLite source" in result.stdout
    assert "Dry-run complete" in result.stdout


def test_cli_missing_target_exits_with_error():
    """No --target and no DATABASE_URL → exit code 2 with a clear message."""
    import os
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    result = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / "migrate_to_postgres.py")],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2
    assert "DATABASE_URL" in result.stderr
