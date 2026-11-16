"""
Client for the WorkerGate governance service (github.com/sidreddy88/worker-gate).

When WORKERGATE_URL is set, BaseAgent.register_tool() routes every tool call
through governance instead of running it locally. Governance identifies the
worker, authorizes the call, exchanges the worker's token for a single-tool
token, calls the downstream server and audits it. The worker never holds a
downstream credential.

A worker's identity comes from three attributes on the agent:
  wg_tenant  which customer it works for     (default: WORKERGATE_TENANT or "default")
  wg_worker  which worker it is (its role)   (default: class name, lowercased)
  wg_obo     the user it acts on behalf of   (default: "")
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def enabled() -> bool:
    return bool(os.environ.get("WORKERGATE_URL"))


def _identity(agent: Any) -> tuple[str, str, str]:
    return (getattr(agent, "wg_tenant", None) or os.environ.get("WORKERGATE_TENANT", "default"),
            getattr(agent, "wg_worker", None) or type(agent).__name__.lower(),
            getattr(agent, "wg_obo", "") or "")


async def worker_token(agent: Any) -> str:
    """The worker's own short-lived token from the IdP, cached until 30s before expiry."""
    if getattr(agent, "_wg_token", None) and agent._wg_token_exp - 30 > time.time():
        return agent._wg_token
    tenant, worker, obo = _identity(agent)
    idp = os.environ.get("WORKERGATE_IDP", "http://127.0.0.1:9000")
    async with httpx.AsyncClient() as c:
        # shortcut: mock IdP takes client_id without a secret; a real IdP needs per-worker credentials
        r = await c.post(f"{idp}/token", data={"grant_type": "client_credentials",
                                               "client_id": f"{tenant}/{worker}", "obo": obo})
    r.raise_for_status()
    body = r.json()
    agent._wg_token, agent._wg_token_exp = body["access_token"], time.time() + body["expires_in"]
    return agent._wg_token


async def _gate(agent: Any, tool: str, args: dict) -> list:
    headers = {"Authorization": f"Bearer {await worker_token(agent)}"}

    async def go() -> list:
        async with streamablehttp_client(os.environ["WORKERGATE_URL"], headers=headers) as (r, w, _):
            async with ClientSession(r, w) as s:
                await s.initialize()
                res = await s.call_tool(tool, args)
        return [json.loads(c.text) for c in res.content if c.type == "text"]

    # our own deadline: if the gateway dies mid-call, the MCP client logs the dropped connection but leaves the
    # call waiting (minutes). A retry with the same task id is safe: governance runs a write at most once.
    return await asyncio.wait_for(go(), float(os.environ.get("WORKERGATE_TIMEOUT", "30")))


async def list_tools(agent: Any) -> list[dict]:
    """Tools governance lets this worker use, with reviewed descriptions."""
    return await _gate(agent, "list_tools", {})


def _observation(res: dict) -> str:
    """Turn governance's answer into the text the model sees."""
    o = res["outcome"]
    if o == "ok":
        return res["result"] + ("\n(governance read the change back: verified)" if res.get("verified") else "")
    if o == "denied":
        return f"DENIED by governance ({res['stopped_at']}): {res['reason']}. Do not retry this call."
    if o == "pending_approval":
        return (f"PENDING: a human must approve this first (approval {res['approval_id']}: {res['reason']}). "
                f"Stop now and report that approval {res['approval_id']} is pending. Do not retry.")
    if o == "unverified":
        return ("UNVERIFIED: the tool said it worked, but governance read the record back and the change is not "
                "there. Do not report this as done.")
    if o == "infra_error":
        return f"Error: tool unavailable ({res['reason']})."
    return f"Error: {res['result']}"


async def report_outcome(agent: Any, status: str, summary: str) -> None:
    """How the task ended, into governance's audit beside what governance saw it do."""
    await _gate(agent, "call", {"tool": "report_outcome", "args": {"status": status, "summary": summary[:2000]},
                                "task_id": getattr(agent, "task_id", "") or ""})


def via_gateway(agent: Any, name: str):
    """A tool function that sends the call for `name` through governance. Each outcome is also kept on
    `agent.wg_evidence`, so whether the task is done is decided from governance's record, not the model's answer."""
    async def call(**kwargs: Any) -> str:
        meter = getattr(agent, "_wg_meter", None)
        spent = (meter.summary().get("cost_usd") or 0.0) if meter else 0.0

        async def send() -> dict:
            # the model's own reason for this call goes into the audit, labelled as its claim
            [res] = await _gate(agent, "call", {"tool": name, "args": kwargs, "spent_usd": spent,
                                                "task_id": getattr(agent, "task_id", "") or "",
                                                "why": (getattr(agent, "wg_why", "") or "")[:500]})
            return res
        # a durable worker records each call (and waits for approvals) instead of only sending it
        hook = getattr(agent, "wg_durable_call", None)
        res = await hook(name, kwargs, send) if hook else await send()
        if not hasattr(agent, "wg_evidence"):
            agent.wg_evidence = []
        agent.wg_evidence.append({"tool": name, "args": kwargs} | {k: res.get(k) for k in
                                 ("outcome", "verified", "approval_id", "replayed")})
        return _observation(res)
    call.__name__ = f"via_gateway_{name}"
    return call
