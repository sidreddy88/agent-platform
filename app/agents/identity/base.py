"""
IdentityWorker: what the Access, Security Ops and Onboarding workers share.

A worker is a role (its identity at governance), a prompt (harness/<role>/task.prompt)
and nothing else: it has no tools of its own and uses what governance lists for it.

Whether a task is done is decided from governance's evidence, not the model's answer:
  waiting_approval  a call is waiting for a human; rerun with the same --task-id after approval
  needs_review      a write was unverified or failed, or the run hit its turn limit
  done              every write was read back and verified
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path

from app.agents.base import AgentResult, BaseAgent
from app.services import cost_meter, workergate
from app.services.llm import LLMService

WRITES = {"create_user", "add_to_group", "assign_app", "suspend_user"}


async def _not_local(**_: object) -> str:
    raise RuntimeError("identity tools only run through governance")  # replaced by the register_tool hook


class IdentityWorker(BaseAgent):
    role = ""

    def __init__(self, tenant: str, obo: str, task_id: str | None = None, model: str | None = None) -> None:
        super().__init__(llm=LLMService(model=model) if model else None)
        self.wg_tenant, self.wg_worker, self.wg_obo = tenant, self.role, obo
        self.task_id = task_id or uuid.uuid4().hex[:12]
        self.wg_evidence: list[dict] = []
        # day 1 finding: the model invented tool results in one reply; keep only its first action
        self._discard_after_first_action = True
        self._required_tool_names_before_answer = {"get_user", "list_users"}

    async def handle(self, request: str) -> tuple[AgentResult, str]:
        if not workergate.enabled():
            raise RuntimeError(f"{type(self).__name__} needs WORKERGATE_URL: all its tools live behind governance")
        for t in await workergate.list_tools(self):
            self.register_tool(t["name"], _not_local, t["description"])
        prompt = (Path(__file__).parent / "harness" / self.role / "task.prompt").read_text()
        with cost_meter.metered() as self._wg_meter:
            result = await self.run(prompt.format(tenant=self.wg_tenant, obo=self.wg_obo, request=request))
        return result, self.status(result)

    def status(self, result: AgentResult) -> str:
        if any(e["outcome"] == "pending_approval" for e in self.wg_evidence):
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
    p.add_argument("request")
    a = p.parse_args()
    w = cls(a.tenant, a.obo, a.task_id, a.model)
    result, status = asyncio.run(w.handle(a.request))
    if a.json:
        m = w._wg_meter.summary()
        print(json.dumps({"status": status, "answer": result.answer, "task_id": w.task_id, "models": m["models"],
                          "cost_usd": m["cost_usd"], "input_tokens": m["tokens"]["input"],
                          "output_tokens": m["tokens"]["output"], "steps": len(result.steps),
                          "tool_calls": len(w.wg_evidence), "evidence": w.wg_evidence}, default=str))
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
