"""
One-shot data migration: local SQLite + ChromaDB → RDS Postgres + pgvector.

Run this once when cutting the agent platform over to RDS. The script is
idempotent — re-running won't duplicate rows (upserts on conflict keys)
and won't re-embed (ChromaDB already has the embeddings; we just
translate them into pgvector rows).

Usage:
    # Dry-run — connect, report row counts, no writes
    python scripts/migrate_to_postgres.py --dry-run \
        --target postgresql+psycopg://user:pw@rds.example.com:5432/agent_platform

    # Real run
    python scripts/migrate_to_postgres.py \
        --target postgresql+psycopg://user:pw@rds.example.com:5432/agent_platform

    # If DATABASE_URL is already set in .env, --target can be omitted
    python scripts/migrate_to_postgres.py

Source layout (read-only):
    agent_platform.db          — SQLite database, all 6 SQL tables
    .chromadb/                 — ChromaDB persistent dir, two collections

Target (writes):
    The Postgres URL passed via --target (or DATABASE_URL). Schema is
    created via SQLAlchemy MetaData.create_all (idempotent). pgvector
    extension is created on first use.

Safety:
    - SQLite is opened read-only.
    - --dry-run reports counts without writing anything.
    - Postgres tables are created only if missing; existing rows aren't
      deleted, just upserted.
    - Postgres connectivity + pgvector availability are verified up-front;
      the script exits with a clear error before touching anything if
      either fails.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
SQLITE_PATH = REPO_ROOT / "agent_platform.db"
CHROMA_PATH = REPO_ROOT / ".chromadb"

# Make `from app...` imports work whether the script is invoked as
# `python scripts/migrate_to_postgres.py` from any cwd or from a packaged
# image where /app is the root. Without this, an invocation that doesn't
# pass `PYTHONPATH=/app` fails on the dynamic `from app.core import config`
# lines below.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Tables migrated in dependency order (none depend on each other today,
# but keep this list as the canonical migration order).
SQL_TABLES = [
    "incidents",
    "monitor_pr_map",
    "approvals",
    "agent_runs",
    "monitor_records",
    "agent_failures",
]

# ChromaDB collections defined by the application — see app/services/rag.py.
VECTOR_COLLECTIONS = ["codebase", "incidents"]


# ---------------------------------------------------------------------------
# Source readers
# ---------------------------------------------------------------------------

def _read_sqlite_tables(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Read every row from each SQL_TABLES table in the SQLite file.

    Returns {table_name: [row_dict, ...]}. Tables that don't exist
    (older snapshots) come back empty.
    """
    if not path.exists():
        logger.warning("SQLite source %s not found — nothing to migrate.", path)
        return {t: [] for t in SQL_TABLES}

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out: dict[str, list[dict[str, Any]]] = {}
    try:
        for table in SQL_TABLES:
            try:
                rows = list(conn.execute(f"SELECT * FROM {table}"))
                out[table] = [dict(r) for r in rows]
            except sqlite3.OperationalError as exc:
                logger.warning("Skipping %s: %s", table, exc)
                out[table] = []
    finally:
        conn.close()
    return out


def _read_chroma_collections(path: Path) -> dict[str, list[dict[str, Any]]]:
    """Pull every row from each ChromaDB collection.

    Each row is a dict with id / document / metadata / embedding so the
    target backend can reconstruct it without re-calling OpenAI.
    """
    if not path.exists():
        logger.warning("ChromaDB source %s not found — nothing to migrate.", path)
        return {c: [] for c in VECTOR_COLLECTIONS}

    try:
        import chromadb
    except ImportError:
        logger.warning("chromadb not installed — skipping vector migration.")
        return {c: [] for c in VECTOR_COLLECTIONS}

    client = chromadb.PersistentClient(path=str(path))
    out: dict[str, list[dict[str, Any]]] = {}
    for name in VECTOR_COLLECTIONS:
        try:
            coll = client.get_collection(name)
        except Exception:
            logger.info("Collection %s not present in ChromaDB — skipping.", name)
            out[name] = []
            continue

        try:
            raw = coll.get(include=["documents", "metadatas", "embeddings"])
        except Exception as exc:
            logger.warning("Failed to read collection %s: %s", name, exc)
            out[name] = []
            continue

        # ChromaDB returns numpy arrays for embeddings — use `is None` style
        # checks instead of truthy tests to avoid numpy ambiguity errors.
        ids = raw.get("ids")
        docs = raw.get("documents")
        metas = raw.get("metadatas")
        embs = raw.get("embeddings")

        ids = list(ids) if ids is not None else []
        docs = list(docs) if docs is not None else []
        metas = list(metas) if metas is not None else []
        embs = list(embs) if embs is not None else []

        rows: list[dict[str, Any]] = []
        for idx, _id in enumerate(ids):
            emb = embs[idx] if idx < len(embs) else None
            rows.append({
                "id": _id,
                "document": docs[idx] if idx < len(docs) else "",
                "metadata": dict(metas[idx]) if idx < len(metas) and metas[idx] else {},
                "embedding": list(emb) if emb is not None else None,
            })
        out[name] = rows
    return out


# ---------------------------------------------------------------------------
# Target writers
# ---------------------------------------------------------------------------

def _write_sql_tables(target_url: str, tables: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
    """Upsert every row into the target database.

    Reuses the application's `upsert` helper from app.services.database
    so the dialect-specific INSERT ON CONFLICT logic is exercised end-to-
    end. Tables are created via metadata.create_all() if missing.
    """
    # Force the application's database module to use the target URL.
    os.environ["DATABASE_URL"] = target_url
    # Re-import so the new env var is picked up.
    from importlib import reload
    from app.core import config as config_module
    reload(config_module)
    from app.services import database as db_module
    reload(db_module)

    db_module.init_db()

    written: dict[str, int] = {}
    for table_name, rows in tables.items():
        if not rows:
            written[table_name] = 0
            continue

        target_table = getattr(db_module.tables, table_name)
        for row in rows:
            db_module.upsert(target_table, row)
        written[table_name] = len(rows)
        logger.info("Wrote %d rows → %s", len(rows), table_name)

    return written


def _write_vector_collections(
    target_url: str,
    collections: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    """Translate ChromaDB rows into pgvector via the VectorCollection API."""
    os.environ["DATABASE_URL"] = target_url
    from importlib import reload
    from app.core import config as config_module
    reload(config_module)
    from app.services import database as db_module
    reload(db_module)
    from app.services import vector_store as vs_module
    reload(vs_module)

    if db_module.DIALECT != "postgresql":
        logger.warning(
            "Target dialect is %s, not postgresql — vector migration skipped. "
            "Use a postgresql:// URL for --target.", db_module.DIALECT,
        )
        return {c: 0 for c in collections}

    written: dict[str, int] = {}
    for name, rows in collections.items():
        if not rows:
            written[name] = 0
            continue
        coll = vs_module.make_collection(name)
        items = [
            vs_module.VectorItem(
                id=row["id"],
                document=row["document"] or "",
                metadata=row["metadata"] or {},
                embedding=row["embedding"] or [],
            )
            for row in rows
            if row.get("embedding")  # skip rows without an embedding — nothing to insert
        ]
        if not items:
            logger.warning("Collection %s has %d rows but no embeddings — skipping.", name, len(rows))
            written[name] = 0
            continue
        coll.upsert(items)
        written[name] = len(items)
        logger.info("Wrote %d items → vec_%s", len(items), name)
    return written


# ---------------------------------------------------------------------------
# Connectivity sanity
# ---------------------------------------------------------------------------

def _verify_target(target_url: str) -> tuple[bool, str]:
    """Return (ok, message). Confirms target is reachable and pgvector exists."""
    if not target_url.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
        return False, f"target URL must be Postgres; got {target_url!r}"

    try:
        from sqlalchemy import create_engine, text
    except ImportError:
        return False, "sqlalchemy not installed in this venv"

    # Translate bare postgres:// to the SQLAlchemy form (matches database.py logic).
    url = target_url
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url.split("://", 1)[1]
    elif url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
        url = "postgresql+psycopg://" + url.split("://", 1)[1]

    try:
        engine = create_engine(url, pool_pre_ping=True)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            # Try to install pgvector (idempotent). If permissions are wrong,
            # this fails with a clear message.
            try:
                with engine.begin() as bconn:
                    bconn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception as exc:
                return False, (
                    f"pgvector extension cannot be created on target: {exc}. "
                    f"Run `CREATE EXTENSION vector;` as a superuser first, or "
                    f"grant rds_superuser to the migration role on AWS RDS."
                )
        return True, "target reachable, pgvector available"
    except Exception as exc:
        return False, f"could not connect to target: {exc}"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--target",
        default=os.environ.get("DATABASE_URL", ""),
        help="Postgres URL to write to. Defaults to $DATABASE_URL.",
    )
    parser.add_argument(
        "--sqlite",
        default=str(SQLITE_PATH),
        help=f"Path to source SQLite db (default: {SQLITE_PATH}).",
    )
    parser.add_argument(
        "--chromadb",
        default=str(CHROMA_PATH),
        help=f"Path to source ChromaDB dir (default: {CHROMA_PATH}).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report counts, write nothing.")
    parser.add_argument(
        "--skip-vectors",
        action="store_true",
        help="Skip ChromaDB → pgvector migration (SQL tables only).",
    )
    args = parser.parse_args()

    if not args.target:
        print("Error: --target or DATABASE_URL must be set.", file=sys.stderr)
        return 2

    print(f"→ Source SQLite : {args.sqlite}")
    print(f"→ Source Chroma : {args.chromadb}")
    print(f"→ Target        : {args.target.split('@')[-1]}")  # hide password
    print(f"→ Dry-run       : {args.dry_run}")
    print()

    # Phase 0 — connectivity sanity (skipped on dry-run; we still want to
    # check it's the right shape but don't want to require a real DB).
    if not args.dry_run:
        ok, msg = _verify_target(args.target)
        if not ok:
            print(f"✗ Target check failed: {msg}", file=sys.stderr)
            return 3
        print(f"✓ {msg}")
        print()

    # Phase 1 — read sources
    print("Reading SQLite source...")
    t0 = time.time()
    tables = _read_sqlite_tables(Path(args.sqlite))
    sql_total = sum(len(v) for v in tables.values())
    for name, rows in tables.items():
        print(f"  {name:<20s} {len(rows)} rows")
    print(f"  ({sql_total} rows total in {time.time() - t0:.1f}s)")
    print()

    if args.skip_vectors:
        collections: dict[str, list[dict[str, Any]]] = {c: [] for c in VECTOR_COLLECTIONS}
    else:
        print("Reading ChromaDB source...")
        t0 = time.time()
        collections = _read_chroma_collections(Path(args.chromadb))
        for name, rows in collections.items():
            print(f"  {name:<20s} {len(rows)} items")
        print(f"  (read in {time.time() - t0:.1f}s)")
        print()

    if args.dry_run:
        print("Dry-run complete — no writes performed.")
        return 0

    # Phase 2 — write target
    print("Writing SQL tables...")
    t0 = time.time()
    sql_written = _write_sql_tables(args.target, tables)
    for name, count in sql_written.items():
        print(f"  ✓ {name:<20s} {count} rows")
    print(f"  ({sum(sql_written.values())} rows total in {time.time() - t0:.1f}s)")
    print()

    if not args.skip_vectors:
        print("Writing pgvector collections...")
        t0 = time.time()
        vec_written = _write_vector_collections(args.target, collections)
        for name, count in vec_written.items():
            print(f"  ✓ vec_{name:<16s} {count} items")
        print(f"  ({sum(vec_written.values())} items total in {time.time() - t0:.1f}s)")
        print()

    print("Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
