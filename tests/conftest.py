"""
Pytest configuration — runs before any test module is imported.

Clears Langfuse keys so the test suite never sends traces to the real
Langfuse dashboard. Tracing is silently disabled when keys are absent.
"""
import os

# Must be set before app modules are imported (settings are read at import time)
os.environ["LANGFUSE_PUBLIC_KEY"] = ""
os.environ["LANGFUSE_SECRET_KEY"] = ""
os.environ["ENVIRONMENT"] = "test"
