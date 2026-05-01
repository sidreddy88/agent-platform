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


@pytest.fixture(autouse=True)
def _isolate_session_logger(monkeypatch):
    """Give each test its own in-memory SessionLogger so tests never write to disk."""
    from app.services import session_logger as sl_module
    monkeypatch.setattr(sl_module, "session_logger", sl_module.SessionLogger())
