"""FIX_SELF_FEEDBACK=1 (fix-optimizer runs only): after the fix loop the agent
answers one question about the setup; failing runs' answers reach the
proposer's evidence."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.fix_generation import FixGenerationAgent
from app.harness_optimizer import evidence, profiles
from app.harness_optimizer.acceptance import CaseResult, EvalResult
from app.models.events import ErrorEvent, EventSource, IncidentState

CONTENT = "function target(x) {\n  return x.value;\n}\n"
FIXED = "function target(x) {\n  return x ? x.value : null;\n}"


def _agent(replies):
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner, agent._repo, agent._github = "o", "r", MagicMock()
    agent._with_harness = MagicMock(return_value="(harness)")
    calls = []
    queue = list(replies)

    async def fake(messages=None, tools=None, system=None, **kw):
        calls.append({"last": messages[-1]["content"], "tool_choice": kw.get("tool_choice", "auto")})
        return queue.pop(0) if queue else ("", [], "end_turn")

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake)
    return agent, calls


async def _run(agent):
    incident = IncidentState(error_event=ErrorEvent(source=EventSource.APPLICATION, error_type="TypeError",
                                                    title="x", description="y", service="s"))
    return await agent._generate_fix(content=CONTENT, function_name="target", incident=incident,
                                     file_path="src/target.js", context_bundle={"callers": [], "tests": [], "imports": []})


EDIT = ("", [{"id": "e", "name": "apply_edit", "input": {"new_text": FIXED}}], "tool_use")


@pytest.mark.asyncio
async def test_feedback_question_only_with_the_flag(monkeypatch):
    monkeypatch.delenv("FIX_SELF_FEEDBACK", raising=False)
    agent, calls = _agent([EDIT, ("done", [], "end_turn")])
    await _run(agent)
    assert len(calls) == 2 and agent._self_feedback is None


@pytest.mark.asyncio
async def test_feedback_is_asked_without_tools_and_stored(monkeypatch):
    monkeypatch.setenv("FIX_SELF_FEEDBACK", "1")
    agent, calls = _agent([EDIT, ("done", [], "end_turn"), ("The root-cause rules assume JS.", [], "end_turn")])
    _, new, _, _ = await _run(agent)
    assert new == FIXED
    assert "about the setup rather than this bug" in calls[-1]["last"] and calls[-1]["tool_choice"] == "none"
    assert agent._self_feedback == "The root-cause rules assume JS."


def test_failing_runs_feedback_reaches_the_evidence():
    profiles.use("fix")
    try:
        res = EvalResult({"a": CaseResult(passes=0, trials=1), "b": CaseResult(passes=1, trials=1)})
        trajs = [{"instance_id": "a", "trial": 1, "verdict": "FAIL", "detail": "no patch", "steps": [],
                  "self_feedback": "I needed a way to run the code."},
                 {"instance_id": "b", "trial": 1, "verdict": "PASS", "detail": "resolved", "steps": [],
                  "self_feedback": "fine"}]
        text = evidence.build(res, trajs)
    finally:
        profiles.use("diagnosis")
    assert "I needed a way to run the code." in text and "- fine" not in text
