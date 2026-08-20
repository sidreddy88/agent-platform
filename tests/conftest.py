"""
Pytest configuration — runs before any test module is imported.

Clears Langfuse keys so the test suite never sends traces to the real
Langfuse dashboard. Tracing is silently disabled when keys are absent.

Also disables harness doc injection (no file I/O during tests) and prevents
test runs from writing records to logs/agent_sessions.jsonl.
"""
import atexit
import os
import tempfile

import pytest

# Must be set before app modules are imported (settings are read at import time)
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["ENVIRONMENT"] = "test"
# Disable harness doc injection in tests — avoids file I/O and keeps prompts clean
os.environ["HARNESS_DOCS_PATH"] = ""
# GitHubService.__init__ raises eagerly if this is empty, and
# app/services/incident_loop.py constructs a module-level IncidentLoop()
# singleton at import time that builds one — so importing incident_loop (or
# anything that imports it) requires a token to be present, even though
# nothing in the test suite makes a real GitHub call. Force-override (not
# setdefault) so tests are hermetic regardless of whatever real token
# happens to be in the developer's local .env — same reasoning as the
# Langfuse keys above.
os.environ["GITHUB_TOKEN"] = "test-github-token"
# app/services/database.py falls back to sqlite:///agent_platform.db --
# the SAME file the local dev server writes to -- when DATABASE_URL is
# unset. Without this override, any test touching IncidentStore's DB-backed
# methods (create/update/get_open_pr_for_error/get_resolved_for_error, all
# real SQLAlchemy queries, not in-memory dict lookups) reads and writes the
# developer's actual local database, which accumulates real incidents from
# real local dev/replay runs. That's exactly why several IncidentLoop/
# IncidentStore tests were flaky/failing depending on what happened to be
# sitting in agent_platform.db at run time -- not real bugs, an unisolated
# test DB. Point at a fresh temp file instead; database.py's module-level
# init_db() call creates the schema on it automatically at import time.
_test_db_fd, _test_db_path = tempfile.mkstemp(prefix="agent_platform_test_", suffix=".db")
os.close(_test_db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_test_db_path}"
atexit.register(lambda: os.path.exists(_test_db_path) and os.remove(_test_db_path))


@pytest.fixture(autouse=True)
def _isolate_session_logger(monkeypatch):
    """Give each test its own in-memory SessionLogger so tests never write to disk."""
    from app.services import session_logger as sl_module
    monkeypatch.setattr(sl_module, "session_logger", sl_module.SessionLogger())


@pytest.fixture(autouse=True)
def _clear_test_db():
    """Truncate every table in the isolated test DB before each test.

    The DATABASE_URL override above points every test at one shared temp
    SQLite file for the whole pytest session (the engine is a module-level
    singleton created at import time -- swapping it per-test isn't practical).
    Without this, a row written by one test (e.g. IncidentStore.create())
    persists and pollutes the next test that queries the same tables
    (e.g. get_open_pr_for_error() unexpectedly matching it) -- the same
    class of nondeterminism the DATABASE_URL override above was meant to
    eliminate, just scoped to this run instead of the developer's real DB.
    """
    from app.services.database import engine, metadata
    with engine.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())
    yield
