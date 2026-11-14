"""
Access worker: handles requests to give people access to apps and groups.

It has no tools of its own. It asks the governance service (worker-gate) which
tools it may use, and every call goes through governance. Needs WORKERGATE_URL.

Run (with worker-gate's scripts/run_all.sh running):
  WORKERGATE_URL=http://127.0.0.1:8000/mcp \
  python -m app.agents.identity.access --tenant acme --obo alice "Give bob access to github"
"""
from __future__ import annotations

import argparse
import asyncio
import uuid
from pathlib import Path

from app.agents.base import AgentResult, BaseAgent
from app.services import workergate

HARNESS = Path(__file__).parent / "harness" / "access"


async def _not_local(**_: object) -> str:
    raise RuntimeError("identity tools only run through governance")  # replaced by the register_tool hook


class AccessWorker(BaseAgent):
    def __init__(self, tenant: str, obo: str, task_id: str | None = None) -> None:
        super().__init__()
        self.wg_tenant, self.wg_worker, self.wg_obo = tenant, "access", obo
        self.task_id = task_id or uuid.uuid4().hex[:12]
        # evidence gate from BaseAgent: no answer accepted before the user was actually looked up
        self._required_tool_names_before_answer = {"get_user"}
        # first live run: the model invented tool results in one reply; keep only its first action
        self._discard_after_first_action = True

    async def handle(self, request: str) -> AgentResult:
        if not workergate.enabled():
            raise RuntimeError("AccessWorker needs WORKERGATE_URL: all its tools live behind governance")
        for t in await workergate.list_tools(self):
            self.register_tool(t["name"], _not_local, t["description"])
        prompt = (HARNESS / "task.prompt").read_text()
        return await self.run(prompt.format(tenant=self.wg_tenant, obo=self.wg_obo, request=request))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--tenant", required=True)
    p.add_argument("--obo", required=True, help="user the worker acts for")
    p.add_argument("request")
    a = p.parse_args()
    result = asyncio.run(AccessWorker(a.tenant, a.obo).handle(a.request))
    for s in result.steps:
        print(f"[{s.iteration}] {s.action or 'answer'} {s.action_input if s.action else ''}\n    -> {(s.observation or s.answer)[:200]}")
    print("\nANSWER:", result.answer)
