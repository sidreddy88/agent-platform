"""trace_entry / record_step: agents whose entry point isn't BaseAgent.run() get a Langfuse agent span, and their
actions become tool observations inside it. A fake Langfuse client records what would be sent."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from app.services import tracing


class FakeObs:
    def __init__(self, log, kwargs):
        self.log, self.kwargs, self.updates = log, kwargs, []

    def update(self, **kw):
        self.updates.append(kw)


class FakeLangfuse:
    def __init__(self):
        self.observations, self.stack = [], []

    @contextmanager
    def start_as_current_observation(self, **kwargs):
        obs = FakeObs(self, {**kwargs, "parent": self.stack[-1].kwargs["name"] if self.stack else None})
        self.observations.append(obs)
        self.stack.append(obs)
        try:
            yield obs
        finally:
            self.stack.pop()

    def flush(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    lf = FakeLangfuse()
    monkeypatch.setattr(tracing, "_get_client", lambda: lf)
    return lf


class Agent:
    @tracing.trace_entry(input_of=lambda x, flag=False: {"x": x, "flag": flag},
                         output_of=lambda r: {"doubled": r})
    async def act(self, x, flag=False):
        tracing.record_step("lookup", {"x": x}, {"found": True})
        return 2 * x

    @tracing.trace_entry(input_of=lambda x: {"x": x})
    async def boom(self, x):
        raise ValueError("bad input")

    @tracing.trace_entry(input_of=lambda x: 1 / 0, output_of=lambda r: r["missing"])
    async def sloppy_summaries(self, x):
        return {"ok": x}


def test_entry_point_becomes_an_agent_span_with_steps_inside(fake):
    assert asyncio.run(Agent().act(21, flag=True)) == 42
    agent, step = fake.observations
    assert agent.kwargs["name"] == "Agent" and agent.kwargs["as_type"] == "agent"
    assert agent.kwargs["input"] == {"x": 21, "flag": True}
    assert any(u.get("output") == {"doubled": 42} for u in agent.updates)
    assert step.kwargs["as_type"] == "tool" and step.kwargs["name"] == "lookup" and step.kwargs["parent"] == "Agent"
    assert step.updates[0]["output"] == {"found": True}


def test_errors_are_recorded_and_re_raised(fake):
    with pytest.raises(ValueError, match="bad input"):
        asyncio.run(Agent().boom(1))
    assert any(u.get("level") == "ERROR" and u.get("status_message") == "bad input" for u in fake.observations[0].updates)


def test_a_broken_summary_never_breaks_the_agent(fake):
    assert asyncio.run(Agent().sloppy_summaries(5)) == {"ok": 5}
    obs = fake.observations[0]
    assert "trace_summary_error" in obs.kwargs["input"]


def test_without_langfuse_the_agent_just_runs(monkeypatch):
    monkeypatch.setattr(tracing, "_get_client", lambda: None)
    assert asyncio.run(Agent().act(3)) == 6
    tracing.record_step("noop")                     # no client: nothing to do, no error


def test_incident_summary_is_compact_and_log_free():
    class Ev:
        error_type, title, service, description = "APP_CRASHED", "x" * 500, "api", "huge stack trace ..."

    class Inc:
        id, error_event = "inc-1", Ev()
    s = tracing.incident_summary(Inc())
    assert s == {"incident_id": "inc-1", "error_type": "APP_CRASHED", "title": "x" * 200, "service": "api"}
    assert "description" not in s
