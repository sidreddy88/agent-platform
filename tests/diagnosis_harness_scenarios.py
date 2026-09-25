"""Shared fixtures for the harness-directory refactor's behaviour-neutral
check: build a DiagnosisAgent with mocks and capture exactly what it would
send (system prompt, tool descriptions, task prompt) for a few incidents.
tests/fixtures/diagnosis_harness_golden/ holds the pre-refactor outputs."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from app.agents.base import _build_system_prompt
from app.agents.diagnosis import DiagnosisAgent
from app.models.events import ErrorEvent, EventSource, IncidentState

SCENARIOS = {
    "no_logs_plain": dict(
        metadata={}, description="Colorbar with drawedges=True does not draw edges at extremities",
        prior=None),
    "logs_prior_stacktrace": dict(
        metadata={"log_group": "/ecs/app", "pattern": "TypeError"},
        description=("TypeError: Cannot read properties of undefined (reading 'id')\n"
                     "    at handler (/app/routes/users.js:42:17)\n"
                     "    at processTicksAndRejections (node:internal/process/task_queues:95:5)"),
        prior="INC-12: similar TypeError in routes/users.js, fixed by null-guarding req.user"),
    "logs_no_prior": dict(
        metadata={"log_group": "/ecs/app"}, description="DeprecationWarning: strictQuery", prior=None),
    "prior_only": dict(metadata={}, description="E11000 duplicate key error", prior="INC-3: insertMany race"),
}


class _Stop(Exception):
    pass


def make_agent() -> DiagnosisAgent:
    repo = MagicMock(ready=True, pinned=True)
    repo.file_exists.return_value = True
    agent = DiagnosisAgent(github=MagicMock(), local_repo=repo, owner="o", repo="r", rag=None)
    agent._ensure_local_repo = AsyncMock()
    for name, out in (("get_error_samples", "SAMPLES: TypeError at users.js:42"),
                      ("check_still_occurring", "STILL OCCURRING: yes, 3 in last 10 min"),
                      ("get_occurrence_timeline", "TIMELINE: 14:00 x2, 14:05 x1")):
        _, desc = agent._tools[name]
        agent._tools[name] = (AsyncMock(return_value=out), desc)
    return agent


def capture_task_prompt(name: str) -> str:
    sc = SCENARIOS[name]
    agent = make_agent()
    captured = {}

    async def fake_run(prompt):
        captured["prompt"] = prompt
        raise _Stop

    agent.run = fake_run
    event = ErrorEvent(source=EventSource.APPLICATION, error_type="GITHUB_ISSUE", title=name,
                       description=sc["description"], service="svc", metadata=sc["metadata"])
    incident = IncidentState(error_event=event)
    try:
        asyncio.run(agent.diagnose(incident, prior_context=sc["prior"]))
    except _Stop:
        pass
    return captured["prompt"]


def capture_static() -> dict:
    agent = make_agent()
    return {
        "system_prompt": _build_system_prompt(agent._tools),
        "tool_descriptions": {n: d for n, (_, d) in sorted(agent._tools.items())},
        "max_iterations": agent._max_iterations,
    }
