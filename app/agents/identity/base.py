"""
IdentityWorker: what the Access, Security Ops and Onboarding workers share.

A worker is a role (its identity at governance), a prompt (harness/<role>/task.prompt)
and nothing else: it has no tools of its own and uses what governance lists for it.

Whether a task is done is decided from governance's evidence, not the model's answer:
  waiting_approval  a call is waiting for a human; rerun with the same --task-id after approval

Durable mode (WG_JOURNAL=path, the `journaled` package): every model call and governed tool call is journaled, and
a call waiting for approval suspends the task. A resume (--signal approval:<id>=approved) replays the recorded model
answers and tool results instead of asking the model again, so the task continues with exactly what was approved.
Without WG_JOURNAL a resume reruns the task and the model may choose differently (finding 18 in worker-gate).
  needs_review      a write was unverified or failed, or the run hit its turn limit
  done              every write was read back and verified
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from pathlib import Path

from app.agents.base import AgentResult, BaseAgent
from app.services import cost_meter, workergate
from app.services.llm import LLMService

WRITES = {"create_user", "add_to_group", "assign_app", "suspend_user"}


async def _not_local(**_: object) -> str:
    raise RuntimeError("identity tools only run through governance")  # replaced by the register_tool hook


class _JournaledLLM:
    """The model as a journaled step: its first answer for a turn is recorded, every resume gets that answer back.
    Only the messages and system prompt are checked on replay (a tracing handle differs per run)."""

    def __init__(self, llm, ctx) -> None:
        self._llm, self._ctx = llm, ctx
        self.last_input_tokens = self.last_output_tokens = 0

    def __getattr__(self, name: str):
        return getattr(self._llm, name)

    async def complete(self, messages: list[dict], system: str | None = None, **kw) -> str:
        before = self._ctx.replayed
        out = await self._ctx.astep("llm", lambda m, s: self._llm.complete(messages=m, system=s, **kw),
                                    messages, system)
        replayed = self._ctx.replayed > before  # a replayed turn cost nothing
        self.last_input_tokens = 0 if replayed else self._llm.last_input_tokens
        self.last_output_tokens = 0 if replayed else self._llm.last_output_tokens
        return out


class IdentityWorker(BaseAgent):
    role = ""

    def __init__(self, tenant: str, obo: str, task_id: str | None = None, model: str | None = None) -> None:
        super().__init__(llm=LLMService(model=model) if model else None)
        self.wg_tenant, self.wg_worker, self.wg_obo = tenant, self.role, obo
        self.task_id = task_id or uuid.uuid4().hex[:12]
        self.wg_evidence: list[dict] = []
        self._journal = None          # the journaled.Context while running durably
        self._waiting_for: str | None = None
        # day 1 finding: the model invented tool results in one reply; keep only its first action
        self._discard_after_first_action = True
        self._required_tool_names_before_answer = {"get_user", "list_users"}

    async def handle(self, request: str, signals: dict | None = None) -> tuple[AgentResult, str]:
        if not workergate.enabled():
            raise RuntimeError(f"{type(self).__name__} needs WORKERGATE_URL: all its tools live behind governance")
        for t in await workergate.list_tools(self):
            self.register_tool(t["name"], _not_local, t["description"])
        prompt = (Path(__file__).parent / "harness" / self.role / "task.prompt").read_text()
        text = prompt.format(tenant=self.wg_tenant, obo=self.wg_obo, request=request)
        with cost_meter.metered() as self._wg_meter:
            if os.environ.get("WG_JOURNAL"):
                result = await self._run_durably(text, signals or {})
            else:
                result = await self.run(text)
        return result, self.status(result)

    async def _run_durably(self, text: str, signals: dict) -> AgentResult:
        from journaled import Engine
        eng, real = Engine(os.environ["WG_JOURNAL"]), self._llm
        done: list[AgentResult] = []

        async def flow(ctx, input: dict) -> str:
            self._journal, self._llm, self.wg_evidence = ctx, _JournaledLLM(real, ctx), []  # rebuilt by replay
            done.append(await self.run(input["prompt"]))
            return done[-1].answer

        eng.workflow(self.role)(flow)
        eng.create(self.task_id, self.role, {"prompt": text})  # a known task id keeps its original input
        for name, value in signals.items():
            eng.deliver(self.task_id, name, value)
        try:
            r = await eng.arun(self.task_id)
        finally:
            self._journal, self._llm = None, real
        self.wg_replayed, self.wg_executed = r.replayed, r.executed
        if r.status == "waiting":
            self._waiting_for = r.waiting_for
            return AgentResult(answer=f"suspended: waiting for {r.waiting_for}", steps=[], iterations=0)
        if r.status == "failed":
            raise RuntimeError(f"durable task {self.task_id} failed: {r.error}")
        return done[-1] if done else AgentResult(answer=r.result, steps=[], iterations=0)  # finished earlier

    async def wg_durable_call(self, name: str, kwargs: dict, send) -> dict:
        """Called by the governance hook for every tool call. Durably: the call is a journaled step, and a call
        waiting for approval suspends the task until the decision arrives as a signal."""
        if self._journal is None:
            return await send()
        ctx = self._journal
        res = await ctx.astep(f"tool:{name}", lambda _n, _a: send(), name, kwargs)
        if res.get("outcome") != "pending_approval":
            return res
        self.wg_evidence.append({"tool": name, "args": kwargs, "outcome": "pending_approval", "verified": None,
                                 "approval_id": res["approval_id"], "replayed": None})
        decision = ctx.wait(f"approval:{res['approval_id']}")  # suspends here (a BaseException) until decided
        self.wg_evidence.pop()
        if decision.get("decision") != "approved":
            return {"outcome": "denied", "stopped_at": "approval", "verified": None, "approval_id": res["approval_id"],
                    "replayed": None, "result": None, "reason": f"a person {decision.get('decision', 'rejected')} it"}
        # approved: the same call again, now with the approval on record (a new step: its result is new)
        return await ctx.astep(f"tool:{name}", lambda _n, _a: send(), name, kwargs)

    def status(self, result: AgentResult) -> str:
        if getattr(self, "_waiting_for", None) or any(e["outcome"] == "pending_approval" for e in self.wg_evidence):
            return "waiting_approval"
        bad = [e for e in self.wg_evidence if e["tool"] in WRITES and not (e["outcome"] == "ok" and e["verified"])]
        stopped = result.steps and not result.steps[-1].answer
        return "needs_review" if bad or stopped else "done"


def main(cls: type[IdentityWorker]) -> None:
    p = argparse.ArgumentParser(description=cls.__doc__)
    p.add_argument("--tenant", required=True)
    p.add_argument("--obo", required=True, help="user the worker acts for")
    p.add_argument("--task-id", help="rerun an earlier task (after an approval or a crash)")
    p.add_argument("--model", help="override the default model (for model-switch evals)")
    p.add_argument("--json", action="store_true", help="print one JSON line instead (for evals)")
    p.add_argument("--signal", action="append", default=[], metavar="NAME=DECISION",
                   help="durable mode: deliver a signal, e.g. approval:ab12cd34=approved")
    p.add_argument("request")
    a = p.parse_args()
    w = cls(a.tenant, a.obo, a.task_id, a.model)
    signals = {n: {"decision": d} for n, d in (s.split("=", 1) for s in a.signal)}
    result, status = asyncio.run(w.handle(a.request, signals))
    if a.json:
        m = w._wg_meter.summary()
        print(json.dumps({"status": status, "answer": result.answer, "task_id": w.task_id, "models": m["models"],
                          "cost_usd": m["cost_usd"], "input_tokens": m["tokens"]["input"],
                          "output_tokens": m["tokens"]["output"], "steps": len(result.steps),
                          "tool_calls": len(w.wg_evidence), "evidence": w.wg_evidence,
                          "durable": {"replayed": getattr(w, "wg_replayed", None),
                                      "executed": getattr(w, "wg_executed", None),
                                      "waiting_for": w._waiting_for}}, default=str))
        return
    for s in result.steps:
        print(f"[{s.iteration}] {s.action or 'answer'} {s.action_input if s.action else ''}")
    print(f"\nANSWER: {result.answer}\n\nEVIDENCE (from governance):")
    for e in w.wg_evidence:
        mark = "✓ verified" if e["verified"] else ("replayed" if e["replayed"] else "")
        print(f"  {e['tool']} {e['args']} -> {e['outcome']} {mark} {e['approval_id'] or ''}".rstrip())
    print(f"\nSTATUS: {status}   task {w.task_id}")
    if status == "waiting_approval":
        print(f"After approving, resume with the same command plus: --task-id {w.task_id}")
