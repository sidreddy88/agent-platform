"""Fixed TriageAgent inputs for the harness golden tests: capture what the agent would send the model
(the task prompt, the system prompt, tool descriptions, turn budget) without calling a model.

Regenerate the goldens after a deliberate harness edit:
    python -m tests.triage_harness_scenarios
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
from pathlib import Path
from unittest.mock import MagicMock

from app.agents.base import AgentResult, _build_system_prompt
from app.agents.triage import TriageAgent
from app.models.events import ErrorEvent
from app.services.preferences import build_preferences_prompt

GOLDEN = Path(__file__).parent / "fixtures" / "triage_harness_golden"
DETECTED = _dt.datetime(2026, 11, 2, 9, 30, tzinfo=_dt.timezone.utc)


class _FixedDate(_dt.date):
    @classmethod
    def today(cls):
        return cls(2026, 11, 5)


SCENARIOS: dict[str, dict] = {
    "with_log_group": dict(
        id="evt-1", source="cloudwatch", error_type="TypeError", title="Cannot read properties of undefined",
        description="TypeError: Cannot read properties of undefined (reading 'id') at handler (src/a.js:12)",
        service="api", task_id="task-9", metadata={"log_group": "/ecs/api", "pattern": "TypeError"}),
    "no_log_group": dict(
        id="evt-2", source="application", error_type="", title="Login page blank",
        description="Users report a blank login page after deploy.", service="web", metadata={}),
    "braces_and_newlines": dict(
        id="evt-3", source="cloudwatch", error_type="ValidationError", title="bad body {\"a\": 1}",
        description="line one {x}\nline two {{y}}\twith a tab " + "z" * 150,
        service="api", metadata={"log_group": "/ecs/api"}),
}


def _agent(harness_dir=None) -> TriageAgent:
    kwargs = {"aws": MagicMock(), "store": MagicMock(), "llm": MagicMock()}
    if harness_dir is not None:
        kwargs["harness_dir"] = harness_dir
    return TriageAgent(**kwargs)


def capture_task_prompt(name: str, harness_dir=None) -> str:
    """The prompt TriageAgent.triage would pass to run(), on a fixed date."""
    agent = _agent(harness_dir)
    sent: list[str] = []

    async def fake_run(prompt: str) -> AgentResult:
        sent.append(prompt)
        return AgentResult(answer='{"decision": "real", "severity": "P2"}', steps=[], iterations=1)

    agent.run = fake_run
    event = ErrorEvent(detected_at=DETECTED, **SCENARIOS[name])
    real_date = _dt.date
    _dt.date = _FixedDate
    try:
        asyncio.run(agent.triage(event))
    finally:
        _dt.date = real_date
    return sent[0]


def capture_static(harness_dir=None) -> dict:
    agent = _agent(harness_dir)
    # The system prompt starts with the user-preferences block, which is shared by every agent and changed by
    # other tests (global state). Pin only what follows it: the ReAct instructions with triage's tools.
    system, prefs = _build_system_prompt(agent._tools), build_preferences_prompt()
    assert system.startswith(prefs)
    return {
        "system_prompt": system[len(prefs):],
        "tool_descriptions": {name: desc for name, (_fn, desc) in agent._tools.items()},
        "max_iterations": agent._max_iterations,
    }


if __name__ == "__main__":
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name in SCENARIOS:
        (GOLDEN / f"task_prompt__{name}.txt").write_text(capture_task_prompt(name))
    (GOLDEN / "static.json").write_text(json.dumps(capture_static(), indent=1, sort_keys=True) + "\n")
    print(f"wrote {len(SCENARIOS) + 1} files to {GOLDEN}")
