"""
Pytest configuration — runs before any test module is imported.

Clears Langfuse keys so the test suite never sends traces to the real
Langfuse dashboard. Tracing is silently disabled when keys are absent.

Also disables harness doc injection (no file I/O during tests) and prevents
test runs from writing records to logs/agent_sessions.jsonl.
"""
import os

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


@pytest.fixture(autouse=True)
def _isolate_session_logger(monkeypatch):
    """Give each test its own in-memory SessionLogger so tests never write to disk."""
    from app.services import session_logger as sl_module
    monkeypatch.setattr(sl_module, "session_logger", sl_module.SessionLogger())
