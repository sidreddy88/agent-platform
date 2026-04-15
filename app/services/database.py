"""
SQLite database — single source of truth for all platform state.

Tables:
  incidents      — full IncidentState JSON + indexed columns
  monitor_pr_map — resource_id → pr_url dedup map
  approvals      — full ApprovalRequest JSON + indexed columns
  agent_runs     — one row per completed/failed agent run
  monitor_records — MonitorGenerationResult per PR merge

Usage:
    from app.services.database import get_db, init_db

    conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO ...")
        conn.commit()
    finally:
        conn.close()
"""

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path("agent_platform.db")


def get_db() -> sqlite3.Connection:
    """Open a connection with WAL mode and Row factory."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create all tables if they don't exist. Safe to call repeatedly."""
    conn = get_db()
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS incidents (
                id          TEXT PRIMARY KEY,
                status      TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                data        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS monitor_pr_map (
                resource_id TEXT PRIMARY KEY,
                pr_url      TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS approvals (
                id         TEXT PRIMARY KEY,
                status     TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_runs (
                run_id        TEXT PRIMARY KEY,
                agent_name    TEXT NOT NULL,
                incident_id   TEXT,
                status        TEXT NOT NULL,
                started_at    TEXT NOT NULL,
                completed_at  TEXT,
                duration_ms   REAL,
                error_message TEXT,
                tool_calls    INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_agent_runs_started_at
                ON agent_runs (started_at);
            CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_name
                ON agent_runs (agent_name);

            CREATE TABLE IF NOT EXISTS monitor_records (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                repo         TEXT NOT NULL,
                pr_number    INTEGER NOT NULL,
                generated_at TEXT NOT NULL,
                data         TEXT NOT NULL
            );
        """)
        logger.debug("[DB] Tables initialised at %s", DB_PATH)
    finally:
        conn.close()


# Ensure tables exist whenever this module is imported
init_db()
