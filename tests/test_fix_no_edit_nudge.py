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


@pytest.mark.asyncio
async def test_model_is_warned_before_the_turn_budget_runs_out():
    """sphinx-10614: the model read and searched for every turn and never edited."""
    reads = [("", [{"id": f"r{i}", "name": "read_file", "input": {"path": "src/other.js"}}], "tool_use")
             for i in range(fix_generation._MAX_FIX_TURNS)]
    agent, seen = _agent(reads)
    agent._read_file = AsyncMock(return_value=("const other = 1;", "sha"))
    await _run(agent)
    warn_turn = fix_generation._MAX_FIX_TURNS - fix_generation._BUDGET_WARNING_TURNS
    warning = seen[warn_turn][-1]
    assert warning["role"] == "user" and "turns left and have not made an edit" in warning["content"]
    assert not any("turns left" in str(m.get("content")) for m in seen[warn_turn - 1])


@pytest.mark.asyncio
async def test_no_budget_warning_once_an_edit_exists():
    reads = [_edit()] + [("", [{"id": f"r{i}", "name": "read_file", "input": {"path": "src/other.js"}}],
                          "tool_use") for i in range(fix_generation._MAX_FIX_TURNS)]
    agent, seen = _agent(reads)
    agent._read_file = AsyncMock(return_value=("const other = 1;", "sha"))
    await _run(agent)
    assert not any("turns left" in str(m.get("content")) for msgs in seen for m in msgs)


def _agent_capturing(replies):
    """Like _agent, but also records each call's tools and tool_choice."""
    agent, seen = _agent(replies)
    calls: list[dict] = []
    inner = agent._llm.complete_with_tools.side_effect

    async def capture(messages=None, tools=None, system=None, **kwargs):
        calls.append({"tools": [t["name"] for t in tools or []], "tool_choice": kwargs.get("tool_choice", "auto")})
        return await inner(messages=messages, tools=tools, system=system, **kwargs)

    agent._llm.complete_with_tools = AsyncMock(side_effect=capture)
    return agent, seen, calls


@pytest.mark.asyncio
async def test_nudge_turn_requires_an_edit_or_no_edit_tool_call():
    agent, seen, calls = _agent_capturing([("I'd guard x.", [], "end_turn"), _edit(), ("done", [], "end_turn")])
    _, new, _, _ = await _run(agent)
    assert new == FIXED
    assert calls[0]["tool_choice"] == "auto" and "read_file" in calls[0]["tools"]
    assert calls[1] == {"tools": ["apply_edit", "patch_line", "no_edit"], "tool_choice": "required"}
    assert calls[2]["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_no_edit_tool_call_stops_cleanly_with_no_patch():
    agent, seen, calls = _agent_capturing([
        ("hm", [], "end_turn"),
        ("", [{"id": "n1", "name": "no_edit", "input": {"reason": "the bug is in another file"}}], "tool_use"),
        ("should not be reached", [], "end_turn"),
    ])
    old, new, patches, _ = await _run(agent)
    assert (old, new, patches) == ("", "", [])
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_last_turn_is_forced_when_there_is_still_no_edit():
    reads = [("", [{"id": f"r{i}", "name": "read_file", "input": {"path": "src/other.js"}}], "tool_use")
             for i in range(fix_generation._MAX_FIX_TURNS)]
    agent, seen, calls = _agent_capturing(reads)
    agent._read_file = AsyncMock(return_value=("const other = 1;", "sha"))
    await _run(agent)
    assert calls[-1]["tool_choice"] == "required"
    assert all(c["tool_choice"] == "auto" for c in calls[:-1])
