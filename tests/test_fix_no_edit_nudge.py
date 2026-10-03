"""
FixGenerationAgent's fix loop pushes back once when the model ends its turn
without an edit (DeepSeek-V4.1-Flash's only failure mode on the SWE-bench fix
pilot), and accepts a deliberate NO_EDIT without retrying.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import fix_generation
from app.agents.fix_generation import FixGenerationAgent
from app.models.events import ErrorEvent, EventSource, IncidentState

CONTENT = "function target(x) {\n  return x.value;\n}\n"
FIXED = "function target(x) {\n  return x ? x.value : null;\n}"


def _agent(replies: list[tuple[str, list[dict], str]]) -> tuple[FixGenerationAgent, list[list[dict]]]:
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner, agent._repo = "owner", "repo"
    agent._github = MagicMock()
    agent._with_harness = MagicMock(return_value="(harness)")
    seen: list[list[dict]] = []
    queue = list(replies)

    async def fake(messages=None, tools=None, system=None, **_kwargs):
        seen.append(list(messages))
        return queue.pop(0) if queue else ("", [], "end_turn")

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake)
    return agent, seen


def _incident() -> IncidentState:
    return IncidentState(error_event=ErrorEvent(
        source=EventSource.APPLICATION, error_type="TypeError", title="x",
        description="Cannot read properties of undefined (reading 'value')", service="svc"))


async def _run(agent):
    return await agent._generate_fix(
        content=CONTENT, function_name="target", incident=_incident(), file_path="src/target.js",
        context_bundle={"callers": [], "tests": [], "imports": []})


def _edit() -> tuple[str, list[dict], str]:
    return ("", [{"id": "t1", "name": "apply_edit", "input": {"new_text": FIXED}}], "tool_use")


@pytest.mark.asyncio
async def test_nudge_recovers_a_fix_described_in_prose():
    agent, seen = _agent([
        ("The fix is to guard x before reading .value.", [], "end_turn"),   # no edit
        _edit(),                                                           # after the nudge
        ("done", [], "end_turn"),
    ])
    old, new, patches, _ = await _run(agent)
    assert new == FIXED and old.startswith("function target")
    nudge = seen[1][-1]
    assert nudge["role"] == "user" and "without calling apply_edit or patch_line" in nudge["content"]
    assert seen[1][-2] == {"role": "assistant", "content": "The fix is to guard x before reading .value."}


@pytest.mark.asyncio
async def test_no_edit_marker_stops_without_a_nudge():
    agent, seen = _agent([(f"{fix_generation._NO_EDIT_MARKER}: the bug is not in this file", [], "end_turn")])
    old, new, patches, _ = await _run(agent)
    assert (old, new, patches) == ("", "", [])
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_nudge_fires_at_most_once():
    agent, seen = _agent([("thinking", [], "end_turn"), ("still thinking", [], "end_turn")])
    old, new, patches, _ = await _run(agent)
    assert (old, new, patches) == ("", "", [])
    assert len(seen) == 1 + fix_generation._NO_EDIT_NUDGES
