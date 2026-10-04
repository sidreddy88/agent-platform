"""find_callers in the fix loop uses a per-agent call graph when one is set
(offline evals run several repos at once), else the module-level graph."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import fix_generation
from app.agents.fix_generation import FixGenerationAgent
from app.models.events import ErrorEvent, EventSource, IncidentState


def _graph(name: str) -> MagicMock:
    g = MagicMock()
    g.find_callers.return_value = [SimpleNamespace(file_path=f"{name}.py", function_name="caller", line=1)]
    return g


async def _callers_result(agent) -> str:
    seen: list[list[dict]] = []
    replies = [("", [{"id": "c1", "name": "find_callers", "input": {"function_name": "target"}}], "tool_use"),
               ("NO_EDIT: test", [], "end_turn")]

    async def fake(messages=None, tools=None, system=None, **_kwargs):
        seen.append(list(messages))
        return replies.pop(0)

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake)
    agent._with_harness = MagicMock(return_value="(harness)")
    incident = IncidentState(error_event=ErrorEvent(source=EventSource.APPLICATION, error_type="E",
                                                    title="x", description="y", service="s"))
    await agent._generate_fix(content="function target() {}", function_name="target", incident=incident,
                              file_path="src/t.js", context_bundle={"callers": [], "tests": [], "imports": []})
    return next(m["content"] for m in seen[1] if m.get("role") == "tool")


def _agent() -> FixGenerationAgent:
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner, agent._repo, agent._github = "o", "r", MagicMock()
    return agent


@pytest.mark.asyncio
async def test_per_agent_graph_wins(monkeypatch):
    monkeypatch.setattr(fix_generation, "_code_graph", _graph("module"))
    agent = _agent()
    agent._code_graph = _graph("agent")
    assert "agent.py" in await _callers_result(agent)


@pytest.mark.asyncio
async def test_module_graph_when_none_set(monkeypatch):
    monkeypatch.setattr(fix_generation, "_code_graph", _graph("module"))
    assert "module.py" in await _callers_result(_agent())
