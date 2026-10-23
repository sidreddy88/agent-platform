"""BaseAgent injects the target harness docs; TriageAgent clears them (measured, see its docstring)."""
from app.agents.base import BaseAgent
from app.agents.triage import TriageAgent


def _docs_dir(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("target app coding rules")
    (tmp_path / "CONSTRAINTS.md").write_text("never touch billing")
    from app.core.config import settings
    monkeypatch.setattr(settings, "harness_docs_path", str(tmp_path))


def test_default_agent_gets_target_docs(tmp_path, monkeypatch):
    _docs_dir(tmp_path, monkeypatch)
    agent = BaseAgent()
    assert "target app coding rules" in agent._harness_docs
    assert "never touch billing" in agent._harness_docs


def test_triage_opts_out(tmp_path, monkeypatch):
    _docs_dir(tmp_path, monkeypatch)
    agent = TriageAgent(aws=object(), store=object())
    assert agent._harness_docs == ""
    assert agent._with_harness("system prompt") == "system prompt"
