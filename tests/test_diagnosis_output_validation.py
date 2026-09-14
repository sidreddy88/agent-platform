"""
Integration-level tests for DiagnosisAgent's output-validation wiring:
  - _retrieved_file_paths is populated by search_codebase / get_file_contents /
    grep_codebase / verify_symbol_in_repo / find_callers
  - _apply_output_validation forces escalate=True and records evidence on
    failure, and never raises even if the validator itself blows up
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.diagnosis import DiagnosisAgent, DiagnosisResult


def _make_agent() -> DiagnosisAgent:
    agent = DiagnosisAgent.__new__(DiagnosisAgent)
    agent._owner = "owner"
    agent._repo = "repo"
    agent._aws = MagicMock()
    agent._rag = None
    agent._github = MagicMock()
    agent._local_repo = MagicMock(ready=False, pinned=False)
    agent._retrieved_file_paths = set()
    return agent


class TestApplyOutputValidation:
    def test_passing_result_is_untouched(self):
        agent = _make_agent()
        agent._retrieved_file_paths = {"app/services/rag.py"}
        result = DiagnosisResult(
            root_cause="a normal explanation", confidence=0.9,
            affected_file="app/services/rag.py",
        )
        incident = MagicMock(id="inc-1")

        agent._apply_output_validation(result, incident)

        assert result.escalate is False
        assert result.evidence == []

    def test_failing_result_forces_escalate_and_records_evidence(self):
        agent = _make_agent()
        agent._retrieved_file_paths = {"app/services/rag.py"}
        result = DiagnosisResult(
            root_cause="a normal explanation", confidence=0.95,
            affected_file="never_retrieved.py",
        )
        incident = MagicMock(id="inc-2")

        agent._apply_output_validation(result, incident)

        assert result.escalate is True
        assert len(result.evidence) == 1
        assert "OUTPUT VALIDATION WARNING" in result.evidence[0]
        assert "never_retrieved.py" in result.evidence[0]
        # Confidence itself is untouched -- escalate is the routing signal,
        # same convention _enforce_grounding's blast-radius nudge follows.
        assert result.confidence == 0.95

    def test_validator_crash_is_swallowed_not_raised(self, caplog):
        agent = _make_agent()
        result = DiagnosisResult(root_cause="x", confidence=0.9)
        incident = MagicMock(id="inc-3")

        with patch(
            "app.agents.diagnosis.validate_diagnosis_output",
            side_effect=RuntimeError("boom"),
        ):
            with caplog.at_level(logging.ERROR):
                agent._apply_output_validation(result, incident)  # must not raise

        assert result.escalate is False  # untouched on validator crash
        assert any("crashed" in r.message for r in caplog.records)


class TestRetrievedFilePathsTracking:
    """Each read tool should add to _retrieved_file_paths, not just return text."""

    @pytest.mark.asyncio
    async def test_search_codebase_tracks_chunk_file_paths(self):
        agent = _make_agent()
        agent._last_retrieved_chunks = []
        chunk = MagicMock(file_path="app/services/rag.py", start_line=1, end_line=10, score=0.9, content="code")
        rag = MagicMock()
        rag.hybrid_search = AsyncMock(return_value=[chunk])
        agent._rag = rag

        # Re-derive the closure the same way _register_tools builds it, without
        # invoking the full BaseAgent/tool-registration machinery.
        search_codebase = _extract_closure(agent, "search_codebase")
        await search_codebase(query="anything")

        assert "app/services/rag.py" in agent._retrieved_file_paths

    @pytest.mark.asyncio
    async def test_find_callers_tracks_caller_file_paths(self):
        agent = _make_agent()
        caller = MagicMock(file_path="app/services/incident_loop.py", function_name="caller", line=42)
        with patch("app.agents.diagnosis._code_graph") as mock_graph:
            mock_graph.find_callers.return_value = [caller]
            find_callers = _extract_closure(agent, "find_callers")
            await find_callers(function_name="target")

        assert "app/services/incident_loop.py" in agent._retrieved_file_paths


def _extract_closure(agent: DiagnosisAgent, tool_name: str):
    """Build the tool closures the same way _register_tools does, capturing
    just the one function we need for a focused test instead of asserting on
    DiagnosisAgent's full registered-tool surface.
    """
    captured = {}
    original_register_tool = DiagnosisAgent.register_tool

    def fake_register_tool(self, name, fn, description):
        captured[name] = fn

    with patch.object(DiagnosisAgent, "register_tool", fake_register_tool):
        DiagnosisAgent._register_tools(agent)

    return captured[tool_name]
