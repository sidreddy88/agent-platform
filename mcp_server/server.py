"""
Agent Platform MCP Server

Exposes the agent platform's agents as MCP tools so Claude Desktop
(or any MCP client) can invoke them directly.

Tools:
  create_tech_spec        → RequirementsAgent
  review_pr               → CodeReviewAgent
  check_ci_status         → CICDAgent
  check_deployment_health → DeploymentAgent
  diagnose_incident       → IncidentResponseAgent

Run:
  python -m mcp_server.server
  # or via the MCP CLI:
  mcp run mcp_server/server.py
"""

import asyncio
import json
import sys
import os

# Ensure the project root is on the path so `app.*` imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from app.agents.requirements import RequirementsAgent
from app.agents.code_review import CodeReviewAgent
from app.agents.cicd import CICDAgent
from app.agents.deployment import DeploymentAgent
from app.agents.incident import IncidentResponseAgent

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

server = Server("agent-platform")

# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="create_tech_spec",
            description=(
                "Turn a raw product requirement or feature request into a complete "
                "technical specification. Returns: title, overview, functional requirements, "
                "technical approach, API/DB changes, testing strategy, effort estimate, "
                "risks, and open questions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "requirement": {
                        "type": "string",
                        "description": "The raw product requirement, feature description, or ticket text.",
                    }
                },
                "required": ["requirement"],
            },
        ),
        types.Tool(
            name="review_pr",
            description=(
                "Review a GitHub pull request. Fetches the PR diff, analyzes every changed "
                "file for bugs, security issues (SQLi, XSS, hardcoded secrets), performance "
                "problems (N+1, loops), and testing gaps. Returns a structured review with "
                "issues by severity and an APPROVE / REQUEST_CHANGES / NEEDS_DISCUSSION "
                "recommendation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "GitHub repository owner (user or org).",
                    },
                    "repo": {
                        "type": "string",
                        "description": "GitHub repository name.",
                    },
                    "pr_number": {
                        "type": "integer",
                        "description": "Pull request number.",
                    },
                    "post_to_github": {
                        "type": "boolean",
                        "description": "If true, post the review back to the GitHub PR. Default: false.",
                        "default": False,
                    },
                },
                "required": ["owner", "repo", "pr_number"],
            },
        ),
        types.Tool(
            name="check_ci_status",
            description=(
                "Check recent GitHub Actions runs for a repository. Finds the most recent "
                "failure, fetches its logs, classifies the failure type (TEST_FAILURE, "
                "BUILD_ERROR, DEPENDENCY, TIMEOUT, FLAKY_TEST), and suggests a concrete fix."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "owner": {
                        "type": "string",
                        "description": "GitHub repository owner.",
                    },
                    "repo": {
                        "type": "string",
                        "description": "GitHub repository name.",
                    },
                    "run_id": {
                        "type": "integer",
                        "description": "Optional specific run ID to analyze. If omitted, uses the most recent failure.",
                    },
                },
                "required": ["owner", "repo"],
            },
        ),
        types.Tool(
            name="check_deployment_health",
            description=(
                "Check the health of an AWS ECS service. Returns task counts "
                "(running vs desired), deployment status, recent service events, CPU/memory "
                "metrics, and stopped task failure reasons. Detects: task failures, capacity "
                "issues, high CPU, deployment problems."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "cluster": {
                        "type": "string",
                        "description": "ECS cluster name.",
                    },
                    "service": {
                        "type": "string",
                        "description": "ECS service name.",
                    },
                    "log_group": {
                        "type": "string",
                        "description": "Optional CloudWatch log group to include log analysis.",
                        "default": "",
                    },
                },
                "required": ["cluster", "service"],
            },
        ),
        types.Tool(
            name="diagnose_incident",
            description=(
                "Diagnose a production incident. Gathers AWS context (ECS health, metrics, "
                "logs), correlates with recent deployments, searches for similar past incidents, "
                "and produces a structured root cause analysis with risk-rated recommended "
                "actions. HIGH and CRITICAL actions are automatically submitted for human "
                "approval — the response includes the approval request ID."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "Description of the incident or alert text.",
                    },
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                        "description": "Incident severity level.",
                        "default": "high",
                    },
                    "service": {
                        "type": "string",
                        "description": "Primary affected ECS service name.",
                        "default": "",
                    },
                    "cluster": {
                        "type": "string",
                        "description": "ECS cluster name.",
                        "default": "default",
                    },
                    "log_group": {
                        "type": "string",
                        "description": "CloudWatch log group for the affected service.",
                        "default": "",
                    },
                    "time_window": {
                        "type": "integer",
                        "description": "Minutes of history to examine. Default: 30.",
                        "default": 30,
                    },
                },
                "required": ["description"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(
    name: str, arguments: dict
) -> list[types.TextContent]:

    try:
        result = await _dispatch(name, arguments)
        return [types.TextContent(type="text", text=result)]
    except Exception as exc:
        error_msg = f"Error running tool '{name}': {type(exc).__name__}: {exc}"
        return [types.TextContent(type="text", text=error_msg)]


async def _dispatch(name: str, args: dict) -> str:
    if name == "create_tech_spec":
        return await _create_tech_spec(args["requirement"])

    if name == "review_pr":
        return await _review_pr(
            args["owner"],
            args["repo"],
            args["pr_number"],
            args.get("post_to_github", False),
        )

    if name == "check_ci_status":
        return await _check_ci_status(
            args["owner"],
            args["repo"],
            args.get("run_id"),
        )

    if name == "check_deployment_health":
        return await _check_deployment_health(
            args["cluster"],
            args["service"],
            args.get("log_group", ""),
        )

    if name == "diagnose_incident":
        return await _diagnose_incident(args)

    raise ValueError(f"Unknown tool: {name}")


# ---------------------------------------------------------------------------
# Per-tool agent runners
# ---------------------------------------------------------------------------

async def _create_tech_spec(requirement: str) -> str:
    agent = RequirementsAgent()
    result = await agent.run(requirement)
    return result.answer


async def _review_pr(owner: str, repo: str, pr_number: int, post_to_github: bool) -> str:
    agent = CodeReviewAgent()
    payload = json.dumps({
        "owner": owner,
        "repo": repo,
        "pr_number": pr_number,
        "post_to_github": post_to_github,
    })
    result = await agent.run(payload)
    return result.answer


async def _check_ci_status(owner: str, repo: str, run_id: int | None) -> str:
    agent = CICDAgent()
    payload: dict = {"owner": owner, "repo": repo}
    if run_id is not None:
        payload["run_id"] = run_id
    result = await agent.run(json.dumps(payload))
    return result.answer


async def _check_deployment_health(cluster: str, service: str, log_group: str) -> str:
    agent = DeploymentAgent()
    resources = [{"type": "ecs", "cluster": cluster, "service": service}]
    if log_group:
        resources.append({"type": "logs", "log_group": log_group, "minutes": 30})
    payload = json.dumps({
        "question": f"Check health of ECS service {service} in cluster {cluster}",
        "resources": resources,
    })
    result = await agent.run(payload)
    return result.answer


async def _diagnose_incident(args: dict) -> str:
    agent = IncidentResponseAgent()
    payload = {
        "alert": f"[{args.get('severity', 'high').upper()}] {args['description']}",
        "service": args.get("service", ""),
        "cluster": args.get("cluster", "default"),
        "log_group": args.get("log_group", ""),
        "time_window": args.get("time_window", 30),
    }
    result = await agent.run(json.dumps(payload))
    return result.answer


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    asyncio.run(main())
