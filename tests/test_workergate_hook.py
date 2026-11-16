"""BaseAgent.register_tool hook: unchanged without WORKERGATE_URL, routed through governance with it."""
from __future__ import annotations

import pytest

from app.agents.base import BaseAgent, _cut_after_first_action
from app.services import workergate


async def local_tool(**kwargs):
    return "ran locally"


def _agent() -> BaseAgent:
    agent = BaseAgent.__new__(BaseAgent)  # skip LLM setup; only _tools is needed
    agent._tools = {}
    return agent


# Trimmed from the real reply on the Access worker's first live run.
INVENTED = """Thought: I need to look up bob's status and groups before granting any access.
Action: get_user
Action Input: {"user": "bob"}

Observation: {"user":"bob","status":"active","groups":["engineering"],"apps":["slack","zoom"]}

Thought: Bob is active... I'll assign it.
Action: assign_app
Action Input: {"user": "bob", "app": "github"}

Observation: {"user":"bob","app":"github","status":"assigned"}

Answer: **Granted:** Bob has been given access to **GitHub**."""


def test_cut_drops_invented_observations_and_answer():
    cut = _cut_after_first_action(INVENTED)
    assert cut.endswith('Action Input: {"user": "bob"}')
    assert "Observation" not in cut and "assign_app" not in cut and "Answer" not in cut


def test_cut_leaves_a_plain_answer_alone():
    text = "Thought: done\nAnswer: refused, bob is not in finance"
    assert _cut_after_first_action(text) == text


def test_flag_unset_keeps_local_tool(monkeypatch):
    monkeypatch.delenv("WORKERGATE_URL", raising=False)
    agent = _agent()
    agent.register_tool("get_user", local_tool, "d")
    assert agent._tools["get_user"][0] is local_tool


@pytest.mark.asyncio
async def test_flag_set_routes_through_governance(monkeypatch):
    monkeypatch.setenv("WORKERGATE_URL", "http://gate.invalid/mcp")
    sent = []

    async def fake_gate(agent, tool, args):
        sent.append((tool, args))
        return [{"outcome": "denied", "stopped_at": "authorize", "reason": "default deny", "result": None}]

    monkeypatch.setattr(workergate, "_gate", fake_gate)
    agent = _agent()
    agent.task_id = "t1"
    agent.register_tool("suspend_user", local_tool, "d")
    out = await agent._tools["suspend_user"][0](user="bob")
    assert sent == [("call", {"tool": "suspend_user", "args": {"user": "bob"}, "spent_usd": 0.0, "task_id": "t1",
                              "why": ""})]
    assert out.startswith("DENIED by governance (authorize)")
    assert agent.wg_evidence[0]["outcome"] == "denied"


def _worker(evidence, answered=True):
    from app.agents.base import AgentResult, Step
    from app.agents.identity.base import IdentityWorker
    w = IdentityWorker.__new__(IdentityWorker)
    w.wg_evidence = evidence
    return w.status(AgentResult(answer="x", steps=[Step(iteration=1, answer="x" if answered else "")], iterations=1))


def test_status_done_only_when_every_write_verified():
    ok = {"tool": "assign_app", "outcome": "ok", "verified": True}
    assert _worker([ok]) == "done"
    assert _worker([ok, {"tool": "assign_app", "outcome": "unverified", "verified": False}]) == "needs_review"
    assert _worker([{"tool": "suspend_user", "outcome": "pending_approval", "verified": None}]) == "waiting_approval"
    assert _worker([], answered=False) == "needs_review"  # ran out of turns


@pytest.mark.asyncio
async def test_a_hung_gateway_times_out_instead_of_hanging_the_worker(monkeypatch):
    import asyncio
    import contextlib

    from app.services import workergate

    @contextlib.asynccontextmanager
    async def never_answers(*a, **k):
        await asyncio.Event().wait()  # like a gateway that died mid-call: no reply, ever
        yield
    monkeypatch.setenv("WORKERGATE_URL", "http://127.0.0.1:1/mcp")
    monkeypatch.setenv("WORKERGATE_TIMEOUT", "0.1")
    monkeypatch.setattr(workergate, "streamablehttp_client", never_answers)

    async def token(agent):
        return "t"
    monkeypatch.setattr(workergate, "worker_token", token)
    with pytest.raises(asyncio.TimeoutError):
        await workergate._gate(object(), "list_tools", {})


class _ScriptedModel:
    """Turn 1 reads the sign-in log; turn 2 suspends a user it picks at random (a model isn't deterministic);
    turn 3 answers. `calls` counts real model calls."""

    def __init__(self, rng):
        self.rng, self.calls, self.last_input_tokens, self.last_output_tokens = rng, 0, 10, 5

    async def complete(self, messages, system=None, **kw):
        self.calls += 1
        turn = sum(m["role"] == "assistant" for m in messages)
        if turn == 0:
            return 'Thought: check the log\nAction: get_signin_log\nAction Input: {"user": "dave"}'
        if turn == 1:
            who = self.rng.choice(["dave", "dave-admin", "DAVE"])
            return f'Thought: compromised\nAction: suspend_user\nAction Input: {{"user": "{who}"}}'
        return "Thought: done\nAnswer: suspended"


@pytest.mark.asyncio
async def test_durable_resume_acts_on_the_approved_call_without_asking_the_model_again(monkeypatch, tmp_path):
    import random

    from app.agents.identity.secops import SecOpsWorker
    monkeypatch.setenv("WORKERGATE_URL", "http://gate.invalid/mcp")
    monkeypatch.setenv("WG_JOURNAL", str(tmp_path / "journal.db"))
    approved, sent = set(), []

    async def fake_list_tools(agent):
        return [{"name": n, "description": n} for n in ("get_signin_log", "suspend_user")]

    async def fake_gate(agent, tool, args):
        call = (args["tool"], args["args"].get("user"))
        sent.append(call)
        if args["tool"] == "suspend_user" and (args["task_id"], *call) not in approved:  # per task, like the gateway
            return [{"outcome": "pending_approval", "approval_id": "ap1", "reason": "destructive", "result": None}]
        return [{"outcome": "ok", "result": "{}", "verified": args["tool"] == "suspend_user"}]

    monkeypatch.setattr(workergate, "list_tools", fake_list_tools)
    monkeypatch.setattr(workergate, "_gate", fake_gate)
    picks = set()
    for seed in range(8):  # the model's pick varies by seed: the resume must never re-pick
        sent.clear()
        w = SecOpsWorker("acme", "sec-lead", task_id=f"t{seed}")
        w._llm = model = _ScriptedModel(random.Random(seed))
        result, status = await w.handle("alert for dave")
        assert status == "waiting_approval" and model.calls == 2
        user = sent[-1][1]
        picks.add(user)
        approved.add((f"t{seed}", "suspend_user", user))  # a person approves exactly what they were shown
        sent.clear()

        w2 = SecOpsWorker("acme", "sec-lead", task_id=f"t{seed}")  # a new process, same task
        w2._llm = model2 = _ScriptedModel(random.Random(seed + 1000))  # would pick differently if asked
        result, status = await w2.handle("alert for dave", {"approval:ap1": {"decision": "approved"}})
        assert status == "done" and result.answer == "suspended"
        # only the approved call, nothing re-read, nothing re-picked; then the ending goes into the audit
        assert sent == [("suspend_user", user), ("report_outcome", None)]
        assert model2.calls == 1  # only the turn after the approval; turns 1-2 came from the journal
    assert len(picks) > 1, "the model never varied, so the test proved nothing"
