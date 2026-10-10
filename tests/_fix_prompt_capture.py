"""Capture the fix agent's first request (system, first user message, tools) for
fixed inputs. Used to snapshot prompts before moving them into the harness dir."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.agents.fix_generation import FixGenerationAgent
from app.models.events import ErrorEvent, EventSource, IncidentState

CASES = {
    "function": dict(content="function target(x) {\n  return x.value;\n}\n", function_name="target",
                     file_path="src/target.js"),
    "module": dict(content="import os\nVALUE = os.environ['X']\n", function_name="<module>",
                   file_path="pkg/settings.py"),
}


async def capture(case: str) -> dict:
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner, agent._repo, agent._github = "o", "r", MagicMock()
    agent._with_harness = lambda s: s
    seen: dict = {"calls": []}

    async def fake(messages=None, tools=None, system=None, **kw):
        seen["calls"].append({"system": system, "last": messages[-1]["content"],
                              "tools": [[t["name"], t["description"]] for t in tools]})
        return ("", [], "end_turn")

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake)
    incident = IncidentState(error_event=ErrorEvent(source=EventSource.APPLICATION, error_type="TypeError",
                                                    title="t", description="d", service="s"))
    await agent._generate_fix(incident=incident, context_bundle={"callers": [], "tests": [], "imports": []},
                              **CASES[case])
    return seen


def capture_all() -> dict:
    return {c: asyncio.run(capture(c)) for c in CASES}
