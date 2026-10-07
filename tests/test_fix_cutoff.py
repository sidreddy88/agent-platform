"""
A fix-loop response cut off at the output limit ("max_tokens") is not treated as
"stopped without an edit". Before the gateway reported finish_reason "length" as
"max_tokens", 22 SWE-bench fix runs (2026-10-07) were cut off at 8,192 tokens by
DeepSeek's hidden reasoning, nudged once, cut off again, and ended with no patch.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import fix_generation
from app.agents.fix_generation import FixGenerationAgent
from app.models.events import ErrorEvent, EventSource, IncidentState

CONTENT = "function target(x) {\n  return x.value;\n}\n"
FIXED = "function target(x) {\n  return x ? x.value : null;\n}"
CUT = ("", [], "max_tokens")


def _agent(replies):
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner, agent._repo, agent._github = "o", "r", MagicMock()
    agent._with_harness = MagicMock(return_value="(harness)")
    seen, calls = [], []
    queue = list(replies)

    async def fake(messages=None, tools=None, system=None, **kwargs):
        seen.append(list(messages))
        calls.append({"tools": [t["name"] for t in tools or []], "tool_choice": kwargs.get("tool_choice", "auto")})
        return queue.pop(0) if queue else ("", [], "end_turn")

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake)
    return agent, seen, calls


async def _run(agent):
    incident = IncidentState(error_event=ErrorEvent(source=EventSource.APPLICATION, error_type="TypeError",
                                                    title="x", description="y", service="s"))
    return await agent._generate_fix(content=CONTENT, function_name="target", incident=incident,
                                     file_path="src/target.js", context_bundle={"callers": [], "tests": [], "imports": []})


def _edit():
    return ("", [{"id": "t1", "name": "apply_edit", "input": {"new_text": FIXED}}], "tool_use")


@pytest.mark.asyncio
async def test_cut_off_response_gets_the_cutoff_message_not_the_nudge():
    agent, seen, calls = _agent([CUT, _edit(), ("done", [], "end_turn")])
    _, new, _, _ = await _run(agent)
    assert new == FIXED
    msg = seen[1][-1]["content"]
    assert "cut off at the output limit" in msg and "ended your turn without" not in msg
    assert calls[1]["tool_choice"] == "auto"     # not forced: the model may still need to read


@pytest.mark.asyncio
async def test_cut_off_forced_turn_stays_forced():
    agent, seen, calls = _agent([("I'd guard x.", [], "end_turn"), CUT, _edit(), ("done", [], "end_turn")])
    _, new, _, _ = await _run(agent)
    assert new == FIXED
    assert calls[1]["tool_choice"] == "required" and calls[2]["tool_choice"] == "required"


@pytest.mark.asyncio
async def test_gives_up_after_too_many_cutoffs_in_a_row():
    agent, seen, calls = _agent([CUT] * (fix_generation._MAX_CUTOFFS + 5))
    old, new, patches, _ = await _run(agent)
    assert (old, new, patches) == ("", "", [])
    assert len(calls) == fix_generation._MAX_CUTOFFS + 1


@pytest.mark.asyncio
async def test_cutoff_count_resets_after_a_normal_turn():
    read = ("", [{"id": "r", "name": "read_file", "input": {"path": "src/other.js"}}], "tool_use")
    agent, seen, calls = _agent([CUT, CUT, CUT, read, CUT, CUT, CUT, _edit(), ("done", [], "end_turn")])
    agent._read_file = AsyncMock(return_value=("const other = 1;", "sha"))
    _, new, _, _ = await _run(agent)
    assert new == FIXED
