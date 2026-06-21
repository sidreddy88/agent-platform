# Agent Platform — Codebase Architecture Reference

> Full code map generated 2026-06-14. See `memory/project_platform_state.md` for the build timeline.

---

## Directory Structure

```
agent-platform/
├── app/
│   ├── agents/               # 13 AI agent implementations
│   ├── api/
│   │   ├── routes/          # 19 FastAPI route handlers
│   │   ├── websocket.py     # WebSocket endpoint
│   │   └── websocket_dashboard.py
│   ├── core/config.py       # Pydantic-settings from .env
│   ├── evals/               # Golden dataset + eval harness
│   ├── models/events.py     # Pydantic data models
│   └── services/            # 28+ shared services
├── mcp_server/server.py     # MCP server for Claude Desktop
├── tests/                   # 33 test files (~600 tests, all mocked)
├── scripts/                 # 11 measurement/eval scripts
├── frontend/                # React + Vite dashboard
├── infra/                   # Terraform for ECS/ALB/Cloudflare
├── targets/allinterviews/   # External codebase being fixed
├── docs/                    # Architecture docs
└── pyproject.toml, requirements.txt
```

---

## Agents (13 total)

### BaseAgent (`app/agents/base.py`)
- **Pattern:** ReAct loop — Thought → Action → Observation → Answer, max 10 iterations
- **System prompt** includes tool descriptions + user preferences from `CLAUDE.md`
- **Tool registration:** `register_tool(name, async_fn, description)` or `@agent.tool()` decorator
- **Checkpointing:** compresses context at 70% of 200K token limit (Haiku summary)
- **Harness injection:** reads `AGENTS.md` + `CONSTRAINTS.md` from `targets/allinterviews/`
- **Tracing:** `@trace_agent` decorator auto-wires Langfuse spans

### TriageAgent (`app/agents/triage.py`)
- **Model:** Claude Haiku (fast/cheap, runs on every alert)
- **Output:** TriageResult — decision (real/noise/duplicate), severity P0–P3, blast_radius, occurrences_24h, duplicate_pr
- **Tools:** `check_duplicate_pr` (incident_store), `get_occurrence_count` (CloudWatch Logs)

### DiagnosisAgent (`app/agents/diagnosis.py`)
- **Model:** Claude Sonnet
- **Output:** DiagnosisResult — root_cause, confidence (0.0–1.0), affected_file/function, blast_radius list
- **Tools:** `get_error_samples`, `check_still_occurring`, `get_occurrence_timeline`, `search_similar_incidents`, `search_codebase` (RAG), `get_file_contents`, `verify_symbol_in_repo` (GitHub Code Search), `get_file_tree` (GitHub Trees API)
- **Confidence gate:** ≥0.70 → FIXING; <0.70 → AWAITING_APPROVAL
- **Grounding guard:** extracts camelCase identifiers from prose, verifies each in repo, nulls hallucinated paths post-parse, retries when affected_file is None

### FixGenerationAgent (`app/agents/fix_generation.py`)
- **Model:** Claude Sonnet (+ Haiku for test generation)
- **Flow (deterministic, not ReAct):**
  1. Fetch file from GitHub or local clone
  2. LLM extracts old function + generates fixed version
  3. Create GitHub Issue + PR branch
  4. LLM generates test code, commit to same branch
  5. Run fix in Docker sandbox against test suite (retry up to 3×)
  6. Open PR
- **Blast radius guard:** blocks PR if changes violate defined limits
- **Approval gate:** LOW confidence / escalate=True triggers human approval
- **PR base:** always targets "staging" branch

### CodeReviewAgent (`app/agents/code_review.py`)
- **Model:** Claude Sonnet
- **Output:** issues by severity, recommendation: APPROVE/REQUEST_CHANGES/NEEDS_DISCUSSION
- **Tools:** `fetch_pr`, `analyze_file` (per changed file), `generate_review` (post to GitHub)
- **Scope:** flags only added (+) lines or issues directly caused by removed (-) lines
- **AI fix warning:** extra scrutiny if PR title contains "ai-generated-fix"

### MergeDecisionAgent (`app/agents/merge_decision.py`)
- Decides merge_now vs refix_first; security flags scoped only to diff-introduced issues

### ErrorClarityAgent (`app/agents/error_clarity.py`)
- When diagnosis is unclear, generates PR with observability improvements (logging, metrics)

### MonitorGenerationAgent (`app/agents/monitor_generation.py`)
- **Trigger:** on PR merge
- **Tools:** `analyze_pr_diff`, `generate_cloudwatch_alarms` (1 alarm per 75 lines), `generate_do_health_checks`, `create_cloudwatch_alarm` (dry-run gated via `CREATE_MONITORS`)

### Other agents
- **CICDAgent** (`app/agents/cicd.py`) — GitHub Actions failure classification + fix suggestion
- **DeploymentAgent** (`app/agents/deployment.py`) — ECS task counts, CPU/memory, stopped task logs
- **RequirementsAgent** (`app/agents/requirements.py`) — product requirements → tech spec
- **IncidentResponseAgent** (`app/agents/incident.py`) — end-to-end orchestration
- **PerformanceAgent** (`app/agents/performance.py`) — CloudWatch metric regression detection

---

## Services (key ones)

### RAG (`app/services/rag.py`)
- **Embeddings:** OpenAI `text-embedding-3-small` (1536-d)
- **Chunking:** 50-line window, 10-line overlap; JS/TS uses function-boundary chunking
- **Collections:** "codebase" (code chunks) + "incidents" (resolved incidents)
- **Three search modes:**
  1. **Semantic** — pure vector cosine similarity
  2. **Hybrid** — `0.7 × vector + 0.3 × lexical_token_match`, min_score=0.45
  3. **Hybrid RRF** — BM25 (k=20) + vector (k=20) merged via Reciprocal Rank Fusion (k=60)
- **Incident search:** min_score=0.80, n_results up to 3

### Vector Store (`app/services/vector_store.py`)
- **ChromaDB** (dev) — SQLite under `.chromadb/`
- **PgVector** (prod) — SQLAlchemy + pgvector on Postgres RDS
- Common interface: `count()`, `upsert()`, `query()`, `get_by_filter()`, `all_metadata()`, `all_items()`, `delete()`, `clear()`

### LLM (`app/services/llm.py`)
- Thin async Anthropic SDK wrapper; tracks `last_input_tokens` / `last_output_tokens`; circuit-broken

### LLM Gateway (`app/services/llm_gateway.py`)
- Routes agents to different models by task type; tracks per-agent cost/latency; budget alerts at 80%/100%

### GitHub (`app/services/github.py`)
- create PR, get/update files, fetch branches, post reviews, search code, `verify_symbol_in_repo`

### AWS (`app/services/aws.py`)
- CloudWatch logs (search, paged get), ECS (tasks, counts), EC2 (instances), ALB (5xx), Route53, SNS

### Approval (`app/services/approvals.py`)
- Risk levels: LOW (auto), MEDIUM (auto by default), HIGH (pending), CRITICAL (pending + confirm)
- DB-backed in SQLite/Postgres; in-memory cache + DB sync

### Database (`app/services/database.py`)
- Auto-detects SQLite (dev) or Postgres (prod)
- Tables: `incidents`, `monitor_pr_map`, `approvals`, `agent_runs`, `monitor_records`, `agent_failures`, `doc_chunk_registry`

### Incident Store (`app/services/incident_store.py`)
- Lazy-loads from DB on first access, caches in-memory; deduplicates by error_type+service

### Incident Loop (`app/services/incident_loop.py`)
- 6-step pipeline async loop driven by EventQueue
- Dedup strategies: SQL (exact), regression (RAG hard block), RAG (soft hint)

### Tracing (`app/services/tracing.py`)
- Langfuse v4; silently disabled when keys absent; nested spans for LLM calls + tool calls

### Circuit Breaker (`app/services/circuit_breaker.py`)
- States: CLOSED → OPEN → HALF_OPEN; registry keyed by dependency name (`"anthropic_llm"`, `"github_api"`)

### Checkpoint (`app/services/checkpoint.py`)
- Compresses conversation at 70% of 200K token limit; Haiku summarization with fallback

---

## API Routes (19 total)

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | Liveness probe |
| POST | `/incidents/trigger` | Inject test ErrorEvent |
| POST | `/incidents/scan` | On-demand 7-day log scan |
| GET | `/incidents` | List incidents (filterable) |
| GET | `/incidents/{id}` | Fetch single incident |
| POST | `/incidents/{id}/approve-fix` | Human approves fix diff |
| POST | `/incidents/{id}/reject-fix` | Human rejects fix |
| POST | `/incidents/{id}/refix` | Re-run fix with review feedback |
| POST | `/incidents/{id}/reject-refix` | Reject re-fix |
| POST | `/approvals/{id}/approve` | Approve gated action |
| POST | `/approvals/{id}/reject` | Reject gated action |
| GET | `/approvals` | List pending approvals |
| GET | `/dashboard` | Dashboard state (incidents, metrics) |
| POST | `/webhooks/github` | PR open/merge events |
| POST | `/webhooks/cloudwatch-alarm` | SNS → CloudWatch alarm ingest |
| GET | `/metrics` | Agent latency, cost, error rates |
| GET | `/logs` | Recent agent logs |
| POST | `/agents/run` | Manually trigger any agent |
| WS | `/ws` | Live dashboard streaming |

---

## Data Models (Pydantic v2, `app/models/events.py`)

### ErrorEvent
`id, source (cloudwatch|do|cloudflare|application), severity, error_type, task_id, title, description, service, resource_id, category, metadata, detected_at`

### IncidentState (~60 fields)
- **Triage:** `triage_decision, triage_reasoning, blast_radius, occurrences_24h`
- **Diagnosis:** `diagnosis, confidence, reproduction_confirmed, diagnosis_affected_file, diagnosis_affected_function, diagnosis_blast_radius, diagnosis_contract_change`
- **Fix:** `fix_description, pending_fix_file/old/new/branch, pr_url, pr_number, pr_branch, pr_files_changed, pr_test_added`
- **Approval:** `approval_id, human_decision, human_decision_reason`
- **Status:** `IncidentStatus` enum — OPEN, TRIAGING, DIAGNOSING, FIXING, AWAITING_FIX_APPROVAL, REVIEWING, AWAITING_APPROVAL, RESOLVED, REJECTED, NOISE, DUPLICATE, VERIFICATION_FAILED, ...
- **Timing:** `detected_at, triage_completed_at, diagnosis_completed_at, pr_created_at, resolved_at` + `@property mttr_seconds`

### TriageResult
`decision, severity (P0–P3), blast_radius, occurrences_24h, duplicate_pr, reasoning`

### DiagnosisResult
`root_cause, confidence, reproduction_confirmed, affected_file, affected_function, fix_approach, blast_radius, evidence, escalate`

### FixResult
`issue_url, pr_url, pr_number, branch, fix_description, files_changed, test_added, commit_sha, blast_radius_violation, blast_radius_violations, target_file, target_function, confidence, escalate, escalate_reason`

---

## MCP Server (`mcp_server/server.py`)

Exposes 5 tools to Claude Desktop:
1. `create_tech_spec` → RequirementsAgent
2. `review_pr` → CodeReviewAgent (owner, repo, pr_number, post_to_github)
3. `check_ci_status` → CICDAgent
4. `check_deployment_health` → DeploymentAgent
5. `diagnose_incident` → IncidentResponseAgent

---

## Pipeline Flow

```
CloudWatch Alarm
    ↓ SNS → HTTPS
POST /webhooks/cloudwatch-alarm
    ↓
ErrorEvent → EventQueue → IncidentLoop
    │
    ├─ TriageAgent (Haiku)
    │   noise/duplicate → TERMINAL
    │   real → DIAGNOSING
    │
    ├─ DiagnosisAgent (Sonnet)
    │   confidence < 0.70 → AWAITING_APPROVAL (human escalation)
    │   confidence ≥ 0.70 → FIXING
    │
    ├─ FixGenerationAgent (Sonnet)
    │   Docker sandbox (up to 3 retries)
    │   blast_radius_violation → human escalation
    │   success → REVIEWING
    │
    ├─ CodeReviewAgent (Sonnet)
    │   REQUEST_CHANGES → AWAITING_REFIX_APPROVAL
    │   APPROVE → AWAITING_APPROVAL (merge gate)
    │
    ├─ [HUMAN APPROVAL GATE]
    │   approved → merge PR → RESOLVED
    │   rejected → REJECTED
    │
    └─ [ON PR MERGE] MonitorGenerationAgent (async)
        auto-generate CloudWatch alarms
```

---

## Tests (33 files, ~600 tests)

All mocked — no live API calls. Key files:
- `test_rag.py` — chunking, indexing, hybrid search, RRF
- `test_incident_pipeline.py` — end-to-end 6-step flow
- `test_diagnosis_grounding.py` — symbol verification
- `test_blast_radius.py` — call graph analysis
- `test_circuit_breaker.py` — resilience
- `test_vector_store.py` — ChromaDB + pgvector
- `test_eval_runner.py` — evaluation metrics (41 tests)

---

## Key Config (`app/core/config.py`)

```
ANTHROPIC_API_KEY, GITHUB_TOKEN, OPENAI_API_KEY
AWS_REGION, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_BASE_URL
SLACK_WEBHOOK_URL
DATABASE_URL          # sqlite:///agent_platform.db | postgresql+psycopg://...
FIX_TARGET_REPO       # owner/repo for fix PRs (AllInterviews)
HARNESS_DOCS_PATH     # targets/allinterviews
CREATE_MONITORS       # false | true (provisions real CloudWatch alarms)
CLOUDWATCH_ALARM_SNS_TOPIC_ARN
CLOUDWATCH_WEBHOOK_TOKEN
DETECTION_POLL_INTERVAL_SECONDS=300
ALERT_DAILY_BUDGET_USD=10.0
ENVIRONMENT           # production | development | test
```

---

## Key Design Decisions

1. **Push, not poll** — SNS webhooks drop MTTD from ~7 days to single-digit minutes
2. **RAG for candidates, live store for truth** — ChromaDB/pgvector find candidates; Postgres confirms ground truth
3. **Hard blocks vs soft hints** — dedup is hard (drop event); regression context is soft (prompt injection)
4. **Ground every symbol** — DiagnosisAgent runs `verify_symbol_in_repo` to reject fabricated identifiers
5. **Sandbox before PR** — fix runs in Docker against real tests; retries 3× before GitHub noise
6. **Confidence gate** — <0.70 escalates to human rather than creating a bad PR
7. **Backend-agnostic vector store** — same code path works locally (ChromaDB) or prod (pgvector)
8. **Circuit breakers on LLM calls** — prevents cascading failures when APIs degrade
9. **Context checkpointing** — compresses at 70% token limit to stay within window
10. **RLHF logging** — rejections captured to `.preference_pairs.jsonl` for future fine-tuning

---

## Quick Stats

- ~20,000+ lines of Python
- 13 agents, 28+ services, 19 API routes
- 33 test files, ~600 tests (all mocked)
- Embeddings: OpenAI text-embedding-3-small (1536-d)
- Models: Sonnet (diagnosis, fix, review), Haiku (triage, monitor gen)
- MTTR: ~6 min agent-bound + human approval time
- Triage accuracy: 92% on 100-case golden dataset
- False-positive rate: <8%
- Production: app.remediatelabs.io (Cloudflare → ALB → ECS Fargate)
