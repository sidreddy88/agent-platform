# Agent Platform

Multi-agent platform built with FastAPI and the Anthropic SDK. Agents use a ReAct loop (Thought → Action → Observation → Answer) implemented in `BaseAgent`.

## Project Structure

```
app/
  agents/          # Agent implementations (all extend BaseAgent)
  api/routes/      # FastAPI route handlers
  api/websocket.py # WebSocket endpoint for streaming
  core/config.py   # Settings loaded from .env via pydantic-settings
  models/          # Pydantic data models
  services/        # Shared services (LLM, GitHub, AWS, RAG, approvals, tracing)
mcp_server/        # MCP server exposing agents to Claude Desktop
tests/             # pytest test suite
```

## Running the Server

```bash
uvicorn app.main:app --reload
```

## Running Tests

```bash
pytest tests/
```

Tests use mocks — no live API calls required.

## Environment Variables (.env)

```
ANTHROPIC_API_KEY=
GITHUB_TOKEN=
OPENAI_API_KEY=          # used by RAG service (embeddings)
CODEBASE_PATH=           # local path to index with RAG
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
LANGFUSE_PUBLIC_KEY=     # leave blank to disable tracing
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=https://us.cloud.langfuse.com
```

## Key Architectural Patterns

### BaseAgent (`app/agents/base.py`)
All agents extend `BaseAgent`. Register tools with `agent.register_tool(name, async_fn, description)` or the `@agent.tool(name, description)` decorator. Call `await agent.run(user_input)` to execute the ReAct loop.

### Tracing (`app/services/tracing.py`)
Langfuse tracing is wired into `BaseAgent` automatically via the `@trace_agent` decorator. Every LLM call and tool execution is captured as a nested span. Tracing is silently disabled when Langfuse keys are absent.

### Approval System (`app/services/approvals.py`)
HIGH/CRITICAL risk actions must be approved before execution. `ApprovalService` is a module-level singleton shared between agents and the REST API. Approve/reject via `POST /approvals/{id}/approve` or `POST /approvals/{id}/reject`.

### RAG Service (`app/services/rag.py`)
ChromaDB-backed vector store with OpenAI `text-embedding-3-small`. Call `rag.index_codebase()` once to build the index, then `rag.search(query)` to retrieve relevant code snippets.

## Agents

| Agent | File | Description |
|---|---|---|
| RequirementsAgent | `app/agents/requirements.py` | Generates tech specs from product requirements |
| CodeReviewAgent | `app/agents/code_review.py` | Reviews GitHub PRs and posts feedback |
| CICDAgent | `app/agents/cicd.py` | Monitors GitHub Actions, diagnoses failures |
| DeploymentAgent | `app/agents/deployment.py` | Monitors AWS ECS/EC2/CloudWatch health |
| IncidentResponseAgent | `app/agents/incident.py` | Auto-diagnoses production incidents |
| PerformanceAgent | `app/agents/performance.py` | Detects metric regressions via CloudWatch |

## MCP Server

Exposes agents as tools for Claude Desktop:

```bash
python mcp_server/server.py
```

Add to Claude Desktop config:
```json
{
  "mcpServers": {
    "agent-platform": {
      "command": "python",
      "args": ["/path/to/agent-platform/mcp_server/server.py"]
    }
  }
}
```
