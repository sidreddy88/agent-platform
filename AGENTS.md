# AGENTS.md — Coding Agent Reference

Multi-agent incident response platform. FastAPI backend, React frontend, SQLite persistence.
Agents use a ReAct loop to triage, diagnose, and fix production errors automatically.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | Python 3.13, FastAPI 0.115 |
| LLM SDK | `anthropic` — Sonnet 4.6 (default), Haiku 4.5 (TriageAgent) |
| Database | SQLite (`agent_platform.db`) via `app/services/database.py` |
| Vector store | ChromaDB + OpenAI `text-embedding-3-small` (`app/services/rag.py`) |
| AWS | `boto3` — ECS, CloudWatch Logs/Metrics, RDS, ALB, S3 |
| GitHub | `httpx` raw API calls — `app/services/github.py` (no PyGitHub) |
| Frontend | React 18 + TypeScript + Vite (`frontend/`) |
| Test runner | `pytest` + `pytest-asyncio` (all mocked, no live API calls) |

---

## Repository Layout

```
app/
  agents/        Agent implementations (all extend BaseAgent)
  api/routes/    FastAPI route handlers (one file per domain)
  models/events.py  Shared data models — ErrorEvent, IncidentState, enums
  services/      Shared services — LLM, GitHub, AWS, RAG, approvals, pipeline
mcp_server/      MCP server for Claude Desktop
tests/           pytest suite (fully mocked)
frontend/        React dashboard
```

Key file: `app/services/incident_loop.py` — orchestrates every pipeline stage.

---

## Run / Verify

```bash
make setup       # install all dependencies (pip + npm) — run first in a fresh environment
npm run dev      # start FastAPI + Vite dev server
make check       # lint then test (run before every PR)
make test        # pytest tests/ --ignore=tests/test_websocket.py
make lint        # ruff check app/ tests/
```

**Known pre-existing test failures** (do not fix by removing assertions):
- `tests/test_blast_radius.py::TestFixGenerationBlastRadius` (2 tests)
- `tests/test_orchestrator.py::TestPipelineDispatch::test_cloudwatch_incident_fires_enrichment`
- `tests/test_orchestrator.py::TestRouteLog::test_stats_incremented_correctly`

---

## Topic Docs

Read when the task involves the relevant area.

| Doc | Read when... |
|---|---|
| [CONSTRAINTS.md](CONSTRAINTS.md) | Touching any pipeline code — hard MUST/MUST NOT rules |
| [app/agents/ARCHITECTURE.md](app/agents/ARCHITECTURE.md) | Adding/modifying agents or fix generation |
| [app/services/ARCHITECTURE.md](app/services/ARCHITECTURE.md) | Touching services, singletons, or the incident pipeline |
| [DECISIONS.md](DECISIONS.md) | Before making architectural choices |
| [PROGRESS.md](PROGRESS.md) | Starting a new feature or need project context |

---

## Environment Variables (`.env`)

```
ANTHROPIC_API_KEY=       # required — all agents
GITHUB_TOKEN=            # required — CodeReviewAgent, CICDAgent, FixGenerationAgent
OPENAI_API_KEY=          # required for RAG (embeddings)
CODEBASE_PATH=           # directory to index with RAG
FIX_TARGET_REPO=         # org/repo for fix PRs, e.g. "acme/backend"
APPROVAL_BASE_URL=       # base URL for approval links in Slack messages
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
LANGFUSE_PUBLIC_KEY=     # leave blank to disable tracing
LANGFUSE_SECRET_KEY=
LANGFUSE_BASE_URL=https://us.cloud.langfuse.com
ECS_LOG_GROUPS=          # comma-separated list for /incidents/scan
```
