# AGENTS.md — Coding Agent Reference

This file is the primary context document for any AI coding agent working on this
repository. Read it in full before making changes. It documents the tech stack,
architectural contracts, verification commands, and known failure modes.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend runtime | Python 3.13, FastAPI 0.110+ |
| LLM SDK | `anthropic` — Sonnet 4.6 for most agents, Haiku 4.5 for TriageAgent |
| Database | SQLite (`agent_platform.db`) via `app/services/database.py` |
| Vector store | ChromaDB + OpenAI `text-embedding-3-small` (`app/services/rag.py`) |
| AWS | `boto3` — ECS, EC2, CloudWatch Logs, CloudWatch Metrics, RDS, ALB, S3 |
| GitHub | `httpx` (raw API calls) in `app/services/github.py` — no PyGitHub |
| Tracing | Langfuse v4 (`app/services/tracing.py`) — silently disabled when keys absent |
| Frontend | React 18 + TypeScript + Vite (`frontend/`) |
| Test runner | `pytest` + `pytest-asyncio` |

**Run the server:** `npm run dev` (starts both FastAPI and the Vite dev server)  
**Run tests:** `pytest tests/` — all tests are mocked; no live API calls, no network required  
**Type check:** no mypy config; rely on Pydantic model validation at runtime

---

## Repository Layout

```
app/
  agents/        All agent implementations (extend BaseAgent)
  api/
    routes/      FastAPI route handlers (one file per domain)
    websocket.py Chat streaming WebSocket
    websocket_dashboard.py  Real-time dashboard push
  core/config.py Pydantic settings loaded from .env
  models/events.py  All shared data models (ErrorEvent, IncidentState, enums)
  services/      Shared services — LLM, GitHub, AWS, RAG, approvals, tracing, etc.
mcp_server/      MCP server exposing agents to Claude Desktop
tests/           pytest suite (fully mocked)
frontend/        React dashboard
```

The single most important file when modifying incident pipeline behavior:
`app/services/incident_loop.py` — orchestrates every stage from triage to approval.

---

## Architectural Contracts

### BaseAgent (`app/agents/base.py`)

Every agent extends `BaseAgent`. Do not instantiate `BaseAgent` directly.

**Tool registration:**
```python
agent.register_tool("tool_name", async_fn, "description")
# or
@agent.tool("tool_name", "description")
async def my_tool(**kwargs): ...
```

Tool functions must be `async` and accept `**kwargs`. They must return a `str`.
The ReAct parser reads `Thought:`, `Action:`, `Action Input:` (JSON), and `Answer:` from LLM output.
Max 10 iterations per run. Context compression fires automatically when token usage > 70%.

**Do not modify `BaseAgent` or the ReAct loop** unless you are specifically fixing a loop bug.

### Incident Pipeline (EventQueue → IncidentLoop)

```
ErrorEvent
  → EventQueue (async)
  → MasterOrchestrator (routes by event source/type)
      → IncidentLoop._process()
          1. TriageAgent    → real / noise / duplicate
          2. DiagnosisAgent → root_cause, confidence (0–1), escalate flag
          3. Confidence gate: ≥ 0.70 → FIXING; < 0.70 → AWAITING_APPROVAL
          4. FixGenerationAgent → FixResult (issue_url, pr_url, files_changed, …)
          5. Definition of Done gate → REVIEWING or VERIFICATION_FAILED
          6. CodeReviewAgent → posts review to PR
          7. ApprovalService → human merge decision
```

`IncidentStatus` enum (all values, in pipeline order):

| Value | Meaning |
|---|---|
| `open` | Created, not yet triaged |
| `triaging` | TriageAgent running |
| `diagnosing` | DiagnosisAgent running |
| `fixing` | FixGenerationAgent running |
| `awaiting_fix_approval` | Pending diff generated, human must approve before commit |
| `reviewing` | PR created, CodeReviewAgent ran, waiting for human merge decision |
| `awaiting_approval` | Human approval gate (diagnosis escalation or PR merge) |
| `verification_failed` | Definition of Done gate blocked the REVIEWING transition |
| `resolved` | Fix merged |
| `rejected` | Human rejected the fix |
| `noise` | Triage determined not a real incident |
| `duplicate` | Existing open PR already covers this error |

**Never add a new status without updating the frontend** (`frontend/src/types.ts` and the status badge component).

### IncidentState (`app/models/events.py`)

Never remove fields — existing SQLite rows are deserialized via `model_validate_json` and missing fields cause load failures. Always add new fields as `Optional[...] = None` or with a safe default.

Key fields added in the harness engineering session:
- `monitor_id: Optional[str]` — set from `error_event.resource_id` at creation; `None` for manually triggered incidents
- `issue_url: Optional[str]` — GitHub issue URL from `FixResult.issue_url`; stored before the DoD gate runs
- `dod_failed_checks: Optional[Dict[str, str]]` — `{check_name: evidence}` populated on `VERIFICATION_FAILED`

### FixGenerationAgent (`app/agents/fix_generation.py`)

Bypasses the ReAct loop — uses direct LLM calls + GitHub API. The public method is `fix_with_steps(incident)` which returns `tuple[FixResult, list[str]]`. The `fix()` method is a thin wrapper that discards steps.

**When mocking in tests, mock `fix_with_steps`, not `fix`:**
```python
loop._fix_agent.fix_with_steps = AsyncMock(return_value=(fix_result, []))
```

`FixResult` fields that must be stored on `IncidentState` before the DoD gate:
- `fix.pr_url` → `incident.pr_url`
- `fix.pr_number` → `incident.pr_number`
- `fix.files_changed` → `incident.pr_files_changed`
- `fix.issue_url` → `incident.issue_url` ← required for `pr_linked_to_issue` DoD check

All fix branches target `PR_BASE = "staging"`. PRs target staging. **Never fork from main.**
Previously branches were forked from main — PRs showed all unmerged main commits alongside the fix diff.

### Definition of Done Gate (`app/services/dod_checker.py`)

Runs before every `REVIEWING` transition. All three locations that set `REVIEWING` must call the gate:
1. `incident_loop.py` — `_process()` (main pipeline)
2. `incident_loop.py` — `resume_fix()` (after diagnosis-escalation approval)
3. `api/routes/incidents.py` — `approve-fix` endpoint (pending diff committed)

The gate helper is `_apply_dod_gate(incident) -> bool`. Returns `False` if any check fails, sets `VERIFICATION_FAILED`, and the caller must `return` without calling `_run_post_fix()`.

`set_pr_for_resource()` must be called **before** the gate, not after — the `monitor_pr_map_updated` check reads from the same table.

If you add a REVIEWING transition anywhere else, wire the gate there too.

### Preference Logger (`app/services/preference_logger.py`)

JSONL only (`.preference_pairs.jsonl`). One record per human rejection. The `harness_failure_layer` field classifies which layer of the harness caused the bad fix:

| Value | Meaning |
|---|---|
| `task_specification` | Agent misunderstood what to fix |
| `context_provision` | Wrong file fetched, missing callers/imports |
| `execution_environment` | GitHub 404, CloudWatch unavailable, tool failure |
| `verification_feedback` | Fix not verified before PR, test missing |
| `state_management` | Agent lost incident context mid-run |
| `model_capability` | Genuine model failure; no harness change would have caught it |

Field defaults to `None`. Callers pass it explicitly. Filter `execution_environment` rejections out of RLHF training data — they are not model failures.

### ApprovalService (`app/services/approvals.py`)

Module-level singleton shared between the pipeline and REST API. `request_approval()` returns an `ApprovalRequest`. HIGH/CRITICAL requests block until a human POSTs to `/approvals/{id}/approve`. LOW/MEDIUM are auto-approved immediately.

**Risk threshold is configurable** in `CLAUDE.md` under User Preferences (`risk_threshold: medium`). Actions at or above the threshold require approval.

### SQLite (`app/services/database.py`)

`get_db()` opens a new connection. Always close it in `finally`. WAL mode is on. Tables: `incidents`, `approvals`, `agent_runs`, `monitor_pr_map`, `monitor_records`.

When adding a column to an existing table, `ALTER TABLE ... ADD COLUMN` is safe (SQLite ignores it if already present via `IF NOT EXISTS` is not supported — use `try/except`). Never `DROP` or rename columns — deserialization reads columns by name from JSON blobs, not directly.

### Circuit Breaker (`app/services/circuit_breaker.py`)

`cb.call(coro)` takes a **coroutine** (not a function). Call syntax:
```python
result = await cb.call(some_service.method(arg1, arg2))
```
The coroutine is created by the call expression and passed in. Do not pass a function reference.

---

## Verification Commands

After any change to the incident pipeline or agents:

```bash
# Full test suite (mocked, fast, ~60s)
pytest tests/

# Only pipeline tests
pytest tests/test_incident_pipeline.py -v

# Only preference logger tests
pytest tests/test_preference_logger.py -v

# Only DoD checker tests (if test file exists)
pytest tests/test_dod_checker.py -v

# Start the full stack locally
npm run dev
```

**Tests that require a live server** (exclude from CI): `tests/test_websocket.py`.
**Tests with known pre-existing failures** (unrelated to pipeline changes):
- `tests/test_blast_radius.py::TestFixGenerationBlastRadius` — LLM mock responses don't trigger blast radius evaluation
- `tests/test_orchestrator.py::TestPipelineDispatch::test_cloudwatch_incident_fires_enrichment`
- `tests/test_orchestrator.py::TestRouteLog::test_stats_incremented_correctly`

Do not fix these by removing the assertions. They need the underlying service behavior corrected.

---

## Fix Generation — Root Cause Rules

This is the single most common source of bad fixes. Read carefully.

**Never generate a symptom fix.** The four most common symptom patterns:

1. **Exception suppression** — wrapping the crash site in `try/catch` without addressing why the exception occurs.
2. **Input sanitization at the wrong layer** — sanitizing output at the consumer instead of fixing the producer. Example: regex-cleaning JSON inside a parsing function instead of setting `response_format: {type: "json_object"}` at the OpenAI API call site.
3. **Value coercion instead of rejection** — converting bad input to a default value (`int(x) if str(x).isdigit() else 0`) instead of validating at the entry point.
4. **Defensive null checks masking missing initialization** — `if obj && obj.isReady()` instead of ensuring the object is always initialized before use.

**The classifyFields incident (reference case):**
Error was `Unexpected token \ in JSON`. Agent saw the parsing function and added regex sanitization. Correct fix: `response_format: {type: "json_object"}` on the upstream OpenAI call. Lesson: always ask "where does this data come from?" before patching the crash site.

**Stack trace resolution is the only file-finding strategy.** Code search by error type string produces false positives — the error type string can appear in comments or logs in unrelated files. If no file can be resolved from the stack trace, do not attempt a fix.

---

## Common Pitfalls

### Test fixtures for IncidentLoop

`IncidentLoop.__new__(IncidentLoop)` bypasses `__init__`. Always set these manually:
```python
loop._rag = None
loop._dedup_stats = {"sql_dedup": 0, "regression": 0, "rag_hit": 0, "cold_start": 0}
```
And mock `fix_with_steps`, not `fix`:
```python
loop._fix_agent.fix_with_steps = AsyncMock(return_value=(fix_result, []))
```

### `set_pr_for_resource` key format

In `_process()` the dedup key is `"{error_type}:{service}:{description[:100]}"`. In `resume_fix()` it is just `error_event.error_type`. This inconsistency is pre-existing — do not normalize it without checking all test assertions that look up by key.

### Adding a new IncidentStatus

1. Add to `IncidentStatus` enum in `app/models/events.py`
2. Add to `list_active()` in `IncidentStore` if it should show in the active feed
3. Update `frontend/src/types.ts` and the status badge component
4. If it is a terminal status, add to the `closed` set in `get_open_pr_for_error()`

### Context leaking between concurrent incidents

`incident_id_ctx` (`app/agents/base.py`) is a `contextvars.ContextVar`. It links agent runs to their incident. It must be set at the start of every pipeline task (`incident_id_ctx.set(incident.id)`). If you add a new pipeline path that creates agent runs, set this variable.

### GitHubService token

`GitHubService()` raises `ValueError` at construction if `GITHUB_TOKEN` is not set. In tests, mock the service at the import site (`patch("app.services.incident_loop.GitHubService")`) or pass a mock instance to the constructor.

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
