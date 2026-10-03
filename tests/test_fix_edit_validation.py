"""
FixGenerationAgent checks an edit's old text when the tool is called. Before,
patch_line always answered "recorded" and a snippet that wasn't in the file was
dropped later with only a log warning; a wrong apply_edit old_text failed the
whole fix after the loop ended. Now the model is told at once, with the closest
lines from the file, and can correct it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.fix_generation import FixGenerationAgent, _nearest_lines
from app.models.events import ErrorEvent, EventSource, IncidentState

# Python: the JS function extractor can't pre-extract it, so apply_edit needs old_text.
PY = "import os\n\n\ndef target(x):\n    return x.value\n\n\ndef other():\n    return 1\n"


def _agent(replies):
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner, agent._repo, agent._github = "o", "r", MagicMock()
    agent._with_harness = MagicMock(return_value="(harness)")
    seen: list[list[dict]] = []
    queue = list(replies)

    async def fake(messages=None, tools=None, system=None, **_kwargs):
        seen.append(list(messages))
        return queue.pop(0) if queue else ("", [], "end_turn")

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake)
    return agent, seen


async def _run(agent):
    incident = IncidentState(error_event=ErrorEvent(source=EventSource.APPLICATION, error_type="AttributeError",
                                                    title="x", description="y", service="s"))
    return await agent._generate_fix(content=PY, function_name="target", incident=incident,
                                     file_path="pkg/mod.py", context_bundle={"callers": [], "tests": [], "imports": []})


def _call(name: str, **inputs) -> tuple[str, list[dict], str]:
    return ("", [{"id": name, "name": name, "input": inputs}], "tool_use")


def _tool_results(seen: list[list[dict]]) -> list[str]:
    return [m["content"] for m in seen[-1] if m.get("role") == "tool"]


@pytest.mark.asyncio
async def test_patch_line_with_a_snippet_not_in_the_file_is_rejected_at_the_call():
    agent, seen = _agent([
        _call("patch_line", old_snippet="    return x.vlaue", new_snippet="    return x and x.value"),
        _call("patch_line", old_snippet="    return x.value", new_snippet="    return x and x.value"),
        ("done", [], "end_turn"),
    ])
    old, new, patches, _ = await _run(agent)
    first, second = _tool_results(seen)
    assert first.startswith("ERROR: patch_line was NOT recorded") and "5:     return x.value" in first
    assert second.startswith("✓ patch_line recorded")
    assert patches == [("    return x.value", "    return x and x.value")]


@pytest.mark.asyncio
async def test_apply_edit_with_wrong_old_text_is_rejected_then_corrected():
    good_old = "def target(x):\n    return x.value"
    agent, seen = _agent([
        _call("apply_edit", old_text="def target(y):\n    return y.value", new_text="def target(x):\n    return x"),
        _call("apply_edit", old_text=good_old, new_text="def target(x):\n    return x"),
        ("done", [], "end_turn"),
    ])
    old, new, patches, _ = await _run(agent)
    first, second = _tool_results(seen)
    assert first.startswith("ERROR: apply_edit was NOT recorded") and "old_text" in first
    assert second.startswith("✓ Primary function fix recorded")
    assert old == good_old and new == "def target(x):\n    return x"


@pytest.mark.asyncio
async def test_whitespace_only_mismatch_says_so():
    agent, seen = _agent([_call("patch_line", old_snippet="\treturn  x.value", new_snippet="\treturn x")])
    await _run(agent)
    assert "except for whitespace or indentation" in _tool_results(seen)[0]


@pytest.mark.asyncio
async def test_patch_line_may_target_the_new_primary_text():
    agent, seen = _agent([
        _call("apply_edit", old_text="def target(x):\n    return x.value", new_text="def target(x):\n    return x.v"),
        _call("patch_line", old_snippet="return x.v", new_snippet="return x.value if x else None"),
        ("done", [], "end_turn"),
    ])
    _, _, patches, _ = await _run(agent)
    assert patches == [("return x.v", "return x.value if x else None")]


def test_nearest_lines_points_at_the_closest_line():
    near = _nearest_lines(PY, "    return x.valu")
    assert "5:     return x.value" in near and "1: import os" not in near
