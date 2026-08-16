# Agent Platform — Technical Architecture (as of 2026-08, verified against code)

*This document supersedes the agent/service descriptions in `README.md` and `CLAUDE.md`, which predate several major features (RAG hybrid search, tree-sitter call graph, LiteLLM gateway, prompt caching, IPI defenses, drift detection) and undercount the agent roster (CLAUDE.md lists 6 agents; the codebase has 12). All claims below were verified by reading the actual source, not by trusting prior docs.*

---

## 1. What it does

Agent Platform is an autonomous incident-remediation system for a production Node.js application (referred to throughout this doc as "the target app"). It watches that application's CloudWatch logs, and when it finds a real error, runs it through an agent pipeline that diagnoses the root cause, writes a code fix, opens a GitHub PR, gets that PR reviewed by another agent, and asks a human to approve the merge — with hard safety gates (blast-radius limits, sandbox test validation, confidence thresholds) at every step that could cause damage.

**Ingestion → approval → pipeline.** Errors enter one of two ways: (a) `DetectionService` polls the configured ECS log groups every 5 minutes and classifies lines via `classify_ecs_log`, or a human triggers `POST /incidents/scan` (7/14/42-day windows) or `/incidents/scan/crashes`; (b) CloudWatch alarms fan out through SNS to `POST /webhooks/cloudwatch-alarm` (the SNS `Notification` handler is currently a documented no-op — see §9). Either way, raw matches land in `PendingEventStore`, an in-memory dedup layer that collapses near-duplicate errors (scrubbing UUIDs/timestamps/numbers into a content signature) and heuristically classifies each as `caught`/`uncaught`/`unknown` (`app/services/pending_events.py`). A human reviews the pending queue in the dashboard and calls `POST /events/{id}/approve` (or `/approve-all`); only then does the event enter the real `asyncio.Queue`-backed `event_queue` that `IncidentLoop`/`Orchestrator` consumes. Crash-category events skip the approval queue and auto-enqueue directly.

**The pipeline itself** (`app/services/incident_loop.py`) is sequential per event: **TriageAgent** (Haiku) classifies `real`/`noise`/`duplicate` and assigns P0–P3 severity — noise and duplicate incidents terminate immediately. Real incidents move to **DiagnosisAgent** (Sonnet), which pulls CloudWatch log context, searches the codebase via RAG and the tree-sitter call graph, and produces a root-cause analysis with a confidence score, gated at `CONFIDENCE_THRESHOLD = 0.70` (`app/agents/diagnosis.py:50`). Below threshold, the incident goes to `AWAITING_APPROVAL` with a Slack approve/reject link (and, if no file was identified, `ErrorClarityAgent` runs to add better logging/error messages so the *next* occurrence is diagnosable). At or above threshold, **FixGenerationAgent** fetches the target file, generates a fix with an internal Anthropic-tool-use loop, runs a self-critique pass (on Haiku) to catch symptom-fixing (null guards instead of real fixes), validates the fix in a sandboxed clone of the target repo (Docker or `npm test` fallback) with up to 3 retry attempts, checks it against `BlastRadiusGuard` (protected paths, file/line-count caps), and opens a GitHub Issue + PR. **CodeReviewAgent** then reviews the PR and posts feedback as a GitHub comment; if it recommends `REQUEST_CHANGES`, **MergeDecisionAgent** (Haiku, single-shot) decides whether the outstanding issues are blocking or the fix can ship now given severity/occurrence volume. Either way, a human approval gate follows (Slack message with approve/reject links via `ApprovalService`, risk-rated HIGH). On human approval — or on GitHub PR merge, which is polled every ~60s and auto-resolves the incident — the incident reaches `RESOLVED` and is indexed into the RAG incident collection for future dedup/context.

**Full state machine** (`IncidentStatus` in `app/models/events.py:49-63`): `OPEN → TRIAGING → {NOISE, DUPLICATE}` (terminal) or `→ DIAGNOSING → AWAITING_APPROVAL` (low-confidence escalation) `→ FIXING → {AWAITING_FIX_APPROVAL, FIX_FAILED}` or `→ REVIEWING → VERIFICATION_FAILED` (DoD gate) or `→ AWAITING_REFIX_APPROVAL` (code review requested changes) or `→ AWAITING_APPROVAL → {RESOLVED, REJECTED}`.

---

## 2. Agents

The codebase has **12 agents** under `app/agents/` (plus `base.py`, the shared ReAct framework). Only 6 of these are documented in `CLAUDE.md`; the other 6 (`TriageAgent`, `DiagnosisAgent`, `FixGenerationAgent`, `MergeDecisionAgent`, `ErrorClarityAgent`, `MonitorGenerationAgent`) are the ones that actually make up the live incident-remediation pipeline described in §1.

| Agent | File | Model | Execution style | Purpose | Invoked from |
|---|---|---|---|---|---|
| `TriageAgent` | `triage.py` | Haiku (`HAIKU_MODEL`) | `BaseAgent` ReAct | Classify real/noise/duplicate + P0–P3 severity | `IncidentLoop._run_triage` |
| `DiagnosisAgent` | `diagnosis.py` | Sonnet (default `LLMService()`) | `BaseAgent` ReAct | Root cause + confidence, with RAG + call-graph + symbol-grounding verification | `IncidentLoop._run_diagnosis` |
| `FixGenerationAgent` | `fix_generation.py` | Sonnet for generation, Haiku (`_llm_haiku`) for self-critique | Sequential orchestration wrapping an internal Anthropic-tool-use agentic loop (`_generate_fix`) | Generate PR: resolve target → tiered-context fix → self-critique → sandbox validate → commit → PR | `IncidentLoop._run_fix` / `resume_fix` / `refix_from_review` |
| `CodeReviewAgent` | `code_review.py` | Default `LLMService()` | Direct sequential calls (no ReAct — deterministic steps) | Fetch PR diff → per-file analysis → assemble review → post to GitHub | `IncidentLoop._run_review`; also `POST /webhooks/github` on PR open/sync |
| `MergeDecisionAgent` | `merge_decision.py` | Haiku | Single LLM call, no tools | BLOCKING vs NON-BLOCKING triage of `REQUEST_CHANGES` review feedback → `merge_now`/`refix_first` | `IncidentLoop._run_post_fix` |
| `ErrorClarityAgent` | `error_clarity.py` | Default `LLMService()` | Hand-rolled tool loop (not `BaseAgent`), max ~6 tool calls | Adds observability (better error messages/logging) when diagnosis confidence is low and no file was identified — explicitly does **not** fix the bug | `IncidentLoop._process` (escalation path, when `affected_file` is null) |
| `MonitorGenerationAgent` | `monitor_generation.py` | Haiku | `BaseAgent`, custom `generate_monitors()` entrypoint | Analyzes a merged PR's diff, generates CloudWatch alarms (1 per 75 LOC) + DO health checks; dry-run gated by `settings.create_monitors` | `POST /webhooks/github` on PR merge (`_run_monitor_generation`) |
| `CICDAgent` | `cicd.py` | Default `LLMService()` | `BaseAgent` ReAct | Diagnose GitHub Actions failures (regex-classified: TEST_FAILURE/BUILD_ERROR/DEPENDENCY/TIMEOUT/FLAKY_TEST) and suggest fixes | Standalone; not wired into `IncidentLoop` |
| `DeploymentAgent` | `deployment.py` | Default `LLMService()` | `BaseAgent` ReAct | ECS/EC2/ALB health checks against hardcoded thresholds (CPU 80/95%, error rate, capacity ratio, ALB 5xx) | Standalone |
| `IncidentResponseAgent` | `incident.py` | Default `LLMService()` | `BaseAgent` ReAct | General-purpose incident diagnosis with regex-based action risk-rating (LOW/MEDIUM/HIGH/CRITICAL) and an `ApprovalService` gate for risky actions | Standalone — **not** the production auto-remediation loop (that's `IncidentLoop`); appears to be an earlier/parallel design that CLAUDE.md's table describes as "auto-diagnoses production incidents," which is now really `TriageAgent`+`DiagnosisAgent` |
| `PerformanceAgent` | `performance.py` | Default `LLMService()` | `BaseAgent` ReAct | Latency/error-rate regression detection vs. 7-day rolling baseline, deployment correlation | Standalone — **its route is commented out in `main.py`**, so it's effectively unreachable via HTTP (see §9) |
| `RequirementsAgent` | `requirements.py` | Default `LLMService()` | `BaseAgent` ReAct | Product requirement → structured tech spec (analyze → estimate → generate) | Standalone, no infra dependencies at all |

**`BaseAgent`** (`app/agents/base.py`) implements the shared ReAct loop: `Thought → Action → Action Input → Observation`, max 10 iterations (`MAX_ITERATIONS`), with brace-counting JSON extraction (not regex) so tool inputs containing nested JS function bodies parse correctly (`_extract_json_block`, lines 111-145). Every agent gets: harness docs (`AGENTS.md`/`CONSTRAINTS.md`) prepended to its system prompt with `cache_control: ephemeral` for prompt caching across ReAct iterations (`_with_harness`, lines 218-232); automatic Langfuse tracing via `@trace_agent`; automatic context-window checkpointing via `context_checkpointer` when input tokens exceed 70% of the context window; and status tracking via `agent_tracker`.

**Cross-cutting design patterns visible across agents:**
- **Model tiering is deliberate**: Haiku for fast/cheap classification (`TriageAgent`, `MergeDecisionAgent`, `MonitorGenerationAgent`, `FixGenerationAgent`'s critique step), Sonnet for deep reasoning (`DiagnosisAgent`, fix generation itself).
- **Deterministic pre-processing before LLM judgment**: `CICDAgent` regex-classifies failure type before asking the LLM to explain it; `DeploymentAgent`/`PerformanceAgent` compute severity/thresholds in Python before the LLM narrates; `IncidentResponseAgent` rates action risk via regex before the approval gate.
- **Verbatim-text-matching as a safety rail**: `ErrorClarityAgent` and `FixGenerationAgent`'s edit-application logic both require LLM-supplied "old" code to literally exist in the file before any edit is applied — skip/fail loudly rather than silently corrupting files.
- **Evidence-accumulator pattern**: `IncidentResponseAgent` (`self._evidence`) and `PerformanceAgent` (`self._metric_data`) both append tool-output strings to an instance list, then hand the concatenated evidence to one final "synthesize" LLM call.

---

## 3. Services (`app/services/`, ~42 files)

### Persistence / data stores
- **`incident_store.py`** — `IncidentStore` singleton. Write-through: in-memory `dict[str, IncidentState]` mirrored to Postgres/SQLite via `database.py`'s `upsert()`. `get_open_pr_for_error()`/`get_resolved_for_error()` deliberately bypass the in-memory cache and hit Postgres directly so dedup is correct across multiple ECS tasks sharing one DB. Auto-migrates a legacy `.incidents.json` file to the DB on first run.
- **`event_queue.py`** — Thin `asyncio.Queue(maxsize=1000)` wrapper. Pure in-memory — unconsumed events are lost on crash/restart.
- **`pending_events.py`** — `PendingEventStore`, pure in-memory. Rich dedup/classification logic (see §1); no DB backing at all.
- **`database.py`** — SQLAlchemy Core layer. Backend auto-detected from `DATABASE_URL` (empty → SQLite WAL-mode; `postgres(ql)://` → rewritten to `postgresql+psycopg://`). Tables: `incidents`, `monitor_pr_map`, `approvals`, `agent_runs`, `monitor_records`, `agent_failures`, `doc_chunk_registry` (RAG), `code_graph_edges`. Dialect-aware `upsert()` (SQLite vs Postgres `ON CONFLICT` syntax differs).
- **`monitor_store.py`** — `MonitorStore`, `deque(maxlen=100)` in-memory ring buffer backed by full Postgres history, loaded on startup.

### LLM / tracing
- **`llm.py`** — `LLMService`, the default single-provider Anthropic client. `MODEL = "claude-sonnet-4-20250514"`, `HAIKU_MODEL = "claude-haiku-4-5-20251001"`, `MAX_TOKENS = 8192`. Own retry-with-backoff (3 retries, exponential + jitter) layered in front of a circuit breaker.
- **`llm_gateway.py`** — `LLMGateway`/`GatewayLLMService`, a LiteLLM-based multi-provider router driven by `config/llm_routing.json`, offering per-task-type model selection (`get_llm_service_for("triage"|"diagnosis"|"fix"|"review")`), cost tracking, and a confidence-based fallback-model retry. Notably enforces a **cross-provider diversity guard** at startup: raises if the `"fix"` and `"review"` task types resolve to the same provider (`_validate_fix_review_providers`) — a deliberate defense against one provider's blind spots reviewing its own fix.
- **`tracing.py`** — Langfuse v4 integration (`trace_agent`, `trace_llm_call`, `trace_tool_call`); silently no-ops when Langfuse keys are absent.
- **`preferences.py`** — Parses the `## User Preferences` block out of `CLAUDE.md` (`@lru_cache`d) and renders it into the system-prompt prefix injected into every agent call — the literal mechanism behind "Agents read this section before every run."

### Reliability
- **`circuit_breaker.py`** — Classic `CLOSED → OPEN → HALF_OPEN → CLOSED` state machine (default: 5 failures to open, 60s timeout, 2 successes to close). Notably careful **single-probe HALF_OPEN gate**: only one call is allowed through while HALF_OPEN; concurrent calls during the probe are rejected like OPEN, preventing a thundering-herd retry storm onto a recovering dependency. Applied to `"anthropic_llm"` and `"github_api"`. In-memory only — resets on restart.
- **`checkpoint.py`** — `ContextCheckpointer`. Compresses ReAct message history via a Haiku summarization call when input tokens exceed 70% of the 200K context window; falls back to raw string concatenation if the LLM call fails.
- **`dod_checker.py`** — `DefinitionOfDoneChecker`, 4 concurrent checks (`pr_has_confidence_score`, `pr_linked_to_issue`, `monitor_pr_map_updated`, `blast_radius_respected` ≤5 files) gating the `REVIEWING` transition.
- **`blast_radius.py`** — `BlastRadiusGuard`: protected-path matching (migrations, auth/secrets, IaC, Dockerfiles, lockfiles, CI configs), `max_files=5`, `max_lines_added=500`, `max_lines_deleted=500` — enforced *before* any PR is opened.
- **`sandbox.py`** — `SandboxService`. Clones the target repo (`--depth=1`), applies two hardcoded compatibility patches, runs a **baseline test pass** on unmodified source, then a **fix test pass**, and only fails the gate on *new* test failures (ignores pre-existing flakiness). Docker-first with an `npm test` fallback (the fallback is what actually runs in prod, since Docker-in-Fargate isn't viable — see Dockerfile notes in §8). 300s timeout.
- **`schema_validator.py`** — `HandoffValidator`. Coerces/validates `TriageResult`/`DiagnosisResult`/`FixResult` at each pipeline handoff boundary; raises `HandoffValidationError` only for truly unrecoverable values (e.g. missing `pr_number` before review).

### RAG / code graph
- **`rag.py`** / **`vector_store.py`** — see §6.
- **`code_graph/{graph,parser,store}.py`** — see §6.

### GitHub / AWS / external integrations
- **`github.py`** — Full GitHub REST wrapper: PRs, diffs, reviews, Actions runs/logs, file contents (base64), code search, branch/issue creation. Handles 409 stale-SHA on `update_file` by retrying with a fresh SHA; handles 422 branch collisions.
- **`github_actions.py`** — A separate, lighter CI-status client (own auth header style) used for dashboard display, distinct from `github.py`'s full API surface.
- **`repo.py`** — `LocalRepoService`: maintains a persistent shallow git clone per-repo for fast local reads (no GitHub API calls for reads); writes still go through `GitHubService`. `ensure_fresh()` always does a `git pull --ff-only` (falls back to `fetch` + `reset --hard` on diverged history) — no TTL/staleness check, so freshness is entirely caller-driven.
- **`aws.py`** — boto3 wrapper: ECS, EC2, ALB, CloudWatch metrics/logs, stack-trace-aware log fetching.
- **`cloudflare_service.py`**, **`digitalocean.py`**, **`mongodb_atlas.py`** — Additional infra integrations (Cloudflare Analytics GraphQL API, DO droplet/health-check API, MongoDB Atlas Admin API) used by the (partially disabled — see §9) unified dashboard.
- **`ipi_guard.py`** — Indirect-prompt-injection defense for untrusted retrieved content (CloudWatch logs, GitHub file contents, RAG chunks). `scan_for_injection()` regex-detects 13 injection phrasings and only **logs a warning** (detection, not blocking); `wrap_untrusted()` is the actual mitigation — wraps content in `<untrusted-content source="...">...</untrusted-content>` with an instruction that the block is data, not commands. Used throughout `DiagnosisAgent` and `FixGenerationAgent`'s tool results.

### Detection / monitoring
- **`detection.py`** — `DetectionService`: polls ECS/EC2/DO/Cloudflare on an interval, emits normalized `ErrorEvent`s.
- **`latency.py`** — `LatencyTracker`: rolling p50/p95/p99 per agent and per pipeline stage (triage/diagnosis/fix/MTTR).
- **`orchestrator.py`** — `MasterOrchestrator`: rule-based (no LLM) event router with priority semaphores and in-flight dedup; owns the actual live `IncidentLoop` instance and drives its `_process()` per dequeued event (see §9 — `IncidentLoop.run_forever()` itself is unused dead code; `Orchestrator.run_forever()` is what main.py starts).
- **`threshold_monitor.py`** — Background loop (default 300s interval) checking DO memory (currently disabled — "focus shifted to triage/diagnosis agents, minimising external pings"), ALB 5xx count, and ECS running-vs-desired count, each with a 30-minute in-memory alert cooldown that resets on restart.

### Evals / dataset / drift
- **`drift_detector.py`** — `DriftDetector`: compares the last-2-days human-approval success rate against a 7-day rolling baseline; fires if it drops >15 points below baseline **or** below an absolute 50% floor. 24h check interval, 24h alert cooldown. Directly points at `.preference_pairs.jsonl` as the remediation ("more negative training examples") — ties detection to the RLHF pipeline.
- **`golden_dataset_builder.py`** — Appends terminal incidents to `app/evals/golden_dataset.jsonl` with a quality filter (always capture human-adjudicated outcomes and duplicates; only capture noise/real cases with new `error_type`s or confidence ≥0.60).
- **`eval_runner.py`** — Runs golden-dataset cases through a real `TriageAgent` with stubbed AWS/incident-store dependencies (no live credentials needed); supports A/B comparison (default Haiku vs. Sonnet) with pass-rate/latency deltas.
- **`failure_injection.py`** — Synthetic chaos-testing: enqueues fabricated events (`false_positive`, `duplicate_alert`, `cascading_failure`) onto the *real* event queue via `POST /injection/trigger`.
- **`preference_logger.py`** — Appends RLHF-style negative training pairs to `.preference_pairs.jsonl` on every human fix rejection, including a `harness_failure_layer` classification (task_specification/context_provision/execution_environment/verification_feedback/state_management/model_capability) — an unusually structured root-cause taxonomy for *agent* failures, not just code failures.

### Observability / session / approvals
- **`session_logger.py`** — One JSONL record per incident to `logs/agent_sessions.jsonl`, including a `harness_compliance` dict that defaults to all-False and only flips True when an agent explicitly marks a doc as read — a lightweight process-compliance audit trail.
- **`agent_tracker.py`** — Live + historical agent-run tracking with hardcoded per-model cost tables (Sonnet 4: $3/$15 per MTok; Haiku 4.5: $0.80/$4 per MTok).
- **`alerting.py`** — `AlertingService`: always logs to console; optionally POSTs Slack Block Kit messages. Provides budget/latency/error-rate threshold-check helpers used elsewhere.
- **`approvals.py`** — `ApprovalService`: LOW auto-approved, MEDIUM auto-approved by default, HIGH/CRITICAL always queue for human decision. In-memory + DB-backed (survives restart, unlike `pending_events.py`).

**Notably clever/unusual pieces worth calling out specifically:** the RAG hybrid search's Reciprocal Rank Fusion (§6); the tree-sitter call graph's `find_callers` blast-radius tool wired into both diagnosis and fix generation (§6); the circuit breaker's single-probe HALF_OPEN gate; the sandbox's baseline-vs-fix regression-only test gating; the `FixGenerationAgent`'s tiered-context prompt (Tier 1 file / Tier 2 callers-tests-as-constraints / Tier 3 imports-as-background) plus its `patch_line` mechanism for verbatim non-primary-function edits; and the drift detector → preference logger → golden dataset closed loop that ties production outcomes back into eval/training data.

---

## 4. Data model (`app/models/events.py`)

**`ErrorEvent`** — the raw detected signal: `source` (cloudwatch/digital_ocean/cloudflare/application), `severity` (null until Triage sets it), `error_type`, `title`, `description`, `service`, `resource_id`, `category` (`"error"` vs `"non_error"` — routes dashboard tabs), free-form `metadata` dict, `detected_at`.

**`IncidentState`** — the full pipeline record, one per incident, keyed by UUID:
- **Triage fields**: `triage_decision`, `triage_reasoning`, `blast_radius` (string descriptor), `occurrences_24h`.
- **Diagnosis fields**: `diagnosis`, `confidence` (0–1), `reproduction_confirmed`, `diagnosis_affected_file`/`_function` (primary fix target), `diagnosis_additional_fix*` (secondary fix location), `diagnosis_blast_radius` (structured list of `{file, function, snippet}` caller entries — the pre-fix-reasoning constraints fed to `FixGenerationAgent`), `diagnosis_contract_change` (`"none"|"signature"|"return_type"|"side_effect"`).
- **Fix fields**: `fix_attempted`, `fix_description`, `pending_fix_*` (diff awaiting human approval before commit), `pr_url`/`pr_number`/`pr_branch`/`pr_files_changed`/`pr_test_added`, `review_posted`, `approval_id`, `merge_decision`/`merge_decision_reasoning`, `clarity_summary`/`clarity_pr_url` (from `ErrorClarityAgent`).
- **Human decision**: `human_decision`, `human_decision_reason`, `human_notes` (code review feedback re-injected into the refix prompt), `outcome`.
- **Post-resolution**: `archived`, `wrong_fix`, `wrong_fix_notes`.
- **Timing** (all UTC): `detected_at`, `triage_completed_at`, `diagnosis_completed_at`, `pr_created_at`, `resolved_at`; derived `mttr_seconds` and `age_seconds` properties.

**`IncidentStatus`** state machine (11 states — see the transition diagram in §1): `OPEN → TRIAGING → {NOISE, DUPLICATE}` | `→ DIAGNOSING → AWAITING_APPROVAL` | `→ FIXING → {AWAITING_FIX_APPROVAL, FIX_FAILED}` | `→ REVIEWING → VERIFICATION_FAILED` | `→ AWAITING_REFIX_APPROVAL` | `→ AWAITING_APPROVAL → {RESOLVED, REJECTED}`.

---

## 5. API surface (`app/api/routes/*.py`, mounted in `app/main.py`)

`main.py` strips a leading `/api` prefix from every request (dev Vite-proxy vs. prod bundle compatibility), mounts routers in this order: `health → websocket(/ws/chat) → ws_dashboard(/ws/dashboard) → approvals → dashboard → incidents → events → webhooks → logs → orchestrator → metrics → circuit_breaker → injection → drift → evals → monitors → agents → debug → sessions → failures`, then mounts the compiled frontend bundle at `/` last. Startup spawns `detection_service`, `threshold_monitor`, `orchestrator`, `drift_detector`, an agent-status websocket broadcaster, and a one-shot RAG backfill as background tasks — **`IncidentLoop.run_forever()` is not started directly; `Orchestrator` owns the live `IncidentLoop` instance and drives it.**

| Router | Prefix | Key endpoints |
|---|---|---|
| `health.py` | (none) | `GET /health` |
| `incidents.py` | `/incidents` | `POST /trigger` (inject test event); `POST /scan`, `/scan/14days`, `/scan/6weeks`, `/scan/crashes` (on-demand log scans, near-duplicate implementations — see §9); `GET ""`, `/active`, `/metrics`, `/{id}`; `DELETE ""`, `/events`, `/{id}`; `POST /{id}/{restart,approve-fix,reject-fix,refix,reject-refix,resolve,mark-merged,unresolve,archive,mark-wrong-fix}` |
| `events.py` | `/events` | `GET /pending`; `POST /{id}/approve`, `/approve-all`, `/{id}/dismiss` — the pending-event human gate |
| `webhooks.py` | `/webhooks` | `POST /github` (PR open/sync → CodeReviewAgent; PR merge → MonitorGenerationAgent); `POST /cloudwatch-alarm` (SNS ingest — see §9 for the current no-op status) |
| `approvals.py` | `/approvals` | `GET /pending`, `""`, `/{id}`; `POST /{id}/approve`, `/{id}/reject` — the `ApprovalService` gate for HIGH/CRITICAL actions |
| `debug.py` | `/debug` | `POST /rag/index`, `POST /code-graph/index` (background full rebuild); `GET /rag`, `/rag/corpus`, `/rag/corpus/codebase` (retrieval-quality inspection) |
| `agents.py` | `/agents` | `GET /status`, `/prs`, `/pr-stats`, `/runs/incident/{id}`; `POST /runs/{id}/note`, `/demo` |
| `monitors.py` | `/monitors` | `GET /generated`, `/coverage`; `POST /generate` |
| `drift.py` | `/drift` | `GET ""`, `/stats` |
| `evals.py` | `/evals` | `POST /run`, `/ab`; `GET /dataset` |
| `circuit_breaker.py` | `/circuit-breakers` | `GET ""`; `POST /{name}/reset` |
| `injection.py` | `/injection` | `GET /scenarios`; `POST /trigger` (chaos testing) |
| `metrics.py` | `/metrics` | `GET /latency`, `/latency/agents`, `/latency/pipeline` |
| `dashboard.py` | `/dashboard` | `GET ""` (aggregate ECS/EC2/ALB/GitHub Actions view; DO/Cloudflare/Atlas pillars disabled) |
| `logs.py`, `sessions.py`, `failures.py`, `orchestrator.py` | `/logs`, `/sessions`, `/failures`, `/orchestrator` | ECS log tail; recent session JSONL; chaos-failure log (append-only, no delete); orchestrator stats/routing table |
| `performance.py` | `/performance` | `GET /heaviest` — **defined but not mounted in `main.py`** (commented out, "Atlas-backed"); dead route |

**Webhook ingestion in detail**: `POST /webhooks/cloudwatch-alarm` handles the three SNS message types — `SubscriptionConfirmation` (auto-confirms by GETting the `SubscribeURL` as a background task), `UnsubscribeConfirmation` (log only), `Notification` (see §9: currently returns `{"status": "ack_noop"}` — real ingestion happens via `DetectionService` polling instead, despite the surrounding code/comments describing a "push-based, zero-polling" design). Optional shared-secret auth via `X-Webhook-Token` header or `?token=` query param, constant-time compared.

**`/debug/code-graph/index`**: uses `settings.codebase_path` if present locally, otherwise shallow-clones `settings.fix_target_repo` via `https://x-access-token:{GITHUB_TOKEN}@github.com/...` into a temp dir; clears and rebuilds all call-graph edges in Postgres. Its own response text documents the limitation that the rebuilt graph only takes effect in `DiagnosisAgent`/`FixGenerationAgent` **after a server restart**, since those modules load the graph into memory once at import time.

---

## 6. RAG and Code Graph

### RAG (`app/services/rag.py`, `vector_store.py`)
- **Embeddings**: OpenAI `text-embedding-3-small`, hardcoded 1536-d.
- **Chunking**: line-window (50 lines, 10-line overlap) by default; a JS/TS-specific chunker splits by brace-matched top-level function boundaries when any are found, falling back to line-window otherwise. A Postgres `doc_chunk_registry` table hash-checks file content so re-indexing only re-embeds changed files.
- **Backend switching**: `vector_store.make_collection()` picks the backend from the DB dialect — `ChromaCollection` (local dev, `.chromadb/` on disk) for SQLite, `PgVectorCollection` (creates the `vector` extension, one `vec_<name>` table per collection) for Postgres. Falls back to an always-empty stub collection if either backend fails to initialize, so the service degrades rather than crashing.
- **Hybrid search**: `hybrid_search_rrf()` runs a vector search (top 20) and BM25 (`rank_bm25.BM25Okapi`, top 20) in parallel, then fuses rankings via Reciprocal Rank Fusion (`k=60`). A simpler additive `hybrid_search()` (`alpha=0.7` vector + lexical) exists as a fallback if `rank_bm25` isn't installed, and the same additive pattern applies to incident search.
- **Two-stage incident retrieval**: `rerank_incidents()` pulls a 20-candidate pool via vector search, then reranks with a cross-encoder (`cross-encoder/ms-marco-MiniLM-L-6-v2`, lazily loaded), falling back to plain vector search if `sentence-transformers` is missing.
- **`min_score` thresholds are deliberately different by use case**: 0.45 for code search, 0.80 for incident hard-block dedup, 0.50 for incident soft-hint context — a false-positive "similar past incident" is treated as more costly than a false-positive code chunk.
- `index_incident()` embeds `"{error_type}: {description[:300]} | Root cause: {diagnosis}"` for every terminal incident with a diagnosis; `IncidentLoop` uses this both as a **hard-block dedup gate** (score ≥0.80 against an open incident → drop the new event, `incident_loop.py:437-454`) and as a **soft context hint** to `DiagnosisAgent` when no exact-match regression is found.

### Code graph (`app/services/code_graph/*.py`)
- **tree-sitter**-based, JS/TS/JSX/TSX only (no Python or other-language call graph support — distinct from RAG's broader language coverage). `parser.py` extracts top-level function definitions and every `call_expression`, binary-searching (`bisect`) each call site into its enclosing top-level function; nested/dynamic calls are intentionally excluded.
- **`graph.py`**'s `CodeGraph` builds both a forward index (caller→callees) and a reverse index (callee→callers) — the reverse index is what makes `find_callers()` an O(1) lookup.
- **Persistence lifecycle**: edges persist to a Postgres `code_graph_edges` table (`code_graph/store.py`). `CodeGraph.load_from_store()` rebuilds the in-memory graph from that table with **no re-parsing needed**. Both `DiagnosisAgent` and `FixGenerationAgent` call this once at **module import time** (`try: CodeGraph.load_from_store() except: CodeGraph()`), so the in-memory graph is read-only for the life of the process — there is no incremental update or hot-reload; a rebuild via `POST /debug/code-graph/index` only takes effect after the next restart (documented explicitly in that endpoint's own response).
- **`find_callers`** is exposed as a tool to both `DiagnosisAgent` (for populating `diagnosis_blast_radius` before recommending a fix — `app/agents/diagnosis.py:651-678`) and `FixGenerationAgent` (to pull caller context into the tiered fix prompt so the LLM doesn't break existing callers — `app/agents/fix_generation.py`, Tier 2 context). Both fall back to `search_codebase`/RAG if the graph has no data for a symbol.
- This feature required a dependency (`tree-sitter`, `tree-sitter-javascript`, `tree-sitter-typescript`) that was **missing from `requirements.txt` in production for a while** — see the bug writeup in §8/§9.

---

## 7. Reliability patterns

- **Circuit breaker** (`app/services/circuit_breaker.py`): 3-state machine (`CLOSED/OPEN/HALF_OPEN`), default 5-failure threshold, 60s open timeout, 2-success close threshold, with a single-probe gate during `HALF_OPEN` so concurrent calls can't stampede a recovering dependency. Applied to `anthropic_llm` (inside `LLMService.complete()`) and `github_api` (wrapping `IncidentLoop._run_fix`/`_run_review`, `incident_loop.py:301-348`).
- **Retry with backoff+jitter** (`app/services/llm.py`): up to 3 retries, exponential backoff from 1.0s capped at 60s, ±50% jitter, retrying only on timeouts/connection errors/429/5xx — explicitly layered *in front of* the circuit breaker so one flaky call doesn't trip the breaker, but repeated exhaustion does.
- **Checkpointing** (`app/services/checkpoint.py`): triggers at 70% of the 200K-token context window, summarizes completed ReAct steps via a cheap Haiku call, and rebuilds the message history as `[user, checkpoint_summary, latest_observation]`.
- **Sandbox validation** (`app/services/sandbox.py`): baseline-vs-fix regression-only test gating in a cloned copy of the target repo, up to 3 fix-regeneration retries fed back with the actual test failure output (`_extract_test_failures`), before falling through to human escalation.
- **Self-critique** (`app/agents/fix_generation.py`, `_critique_fix`): runs on Haiku, checks specifically for symptom-fixing patterns (null guards/optional chaining/try-catch at the crash site instead of fixing the producer function), whether Tier-2 callers still work, and edge-case coverage; emits a mandatory `LOOKS CORRECT`/`NEEDS REVIEW`/`LIKELY WRONG` verdict that the caller uses to trigger an alternate-stack-frame retry on `LIKELY WRONG`.
- **Dedup / idempotency**: three independent layers in `IncidentLoop._process` (`incident_loop.py:395-477`) — (1) SQL check for an already-open PR for the same `error_type`+`service`+`description`; (2) SQL regression check surfacing a prior resolved incident's root cause as diagnosis context; (3) RAG semantic hard-block (score ≥0.80 against a live open incident). A fourth layer sits earlier, in `PendingEventStore`'s content-signature scrubbing before an event even reaches the queue.
- **Safety rails / approval gates**: `ApprovalService` (LOW auto-approved, MEDIUM auto-approved by default, HIGH/CRITICAL always require a human decision) gates diagnosis escalation and every AI-generated fix merge; `BlastRadiusGuard` blocks PR creation outright (before any GitHub write) on protected-path/file-count/line-count violations; `DefinitionOfDoneChecker` blocks the `REVIEWING` transition on 4 checks including a second, independently-defined blast-radius limit.
- **Grounding/anti-hallucination**: `DiagnosisAgent._enforce_grounding()` (`app/agents/diagnosis.py:725-867`) re-verifies every function name and file path the LLM names against the real repo (via GitHub Code Search), nulling out and capping confidence on anything unverified — including a **prose scan** that catches fabricated symbol names leaking into free-text reasoning even when the structured fields pass.
- **Indirect prompt-injection defense** (`app/services/ipi_guard.py`): regex-based detection (logged, not blocking) plus structural `<untrusted-content>` XML-wrapping of all CloudWatch logs, GitHub file contents, and RAG chunks before they reach the LLM.

---

## 8. Infrastructure & deployment

### ECS Fargate stack (`infra/agent_platform/*.tf`)
- **ECS**: a dedicated Fargate cluster (`agent-platform-prod`, `ecs.tf`) — deliberately separate from the target app's production cluster "for blast-radius isolation," ARM64 task definition (~20% cheaper, matches Apple Silicon dev machines natively). Secrets are pulled from SSM Parameter Store via the task execution role (`DATABASE_URL` plus a generic secret map).
- **ALB** (`alb.tf`): HTTP:80 → 301 redirect to HTTPS:443 → forward to the ECS target group; health check on `/health`. Comment documents the TLS chain explicitly: `client → Cloudflare (CF cert) → ALB (ACM cert) → container (HTTP)`.
- **RDS** (`rds.tf`): Postgres 16 with pgvector, `db.t4g.micro` single-AZ, 20GB gp3 (~$13/mo), `publicly_accessible=false` even though it's in a public subnet (no public IP is advertised; a security group additionally restricts ingress to the ECS task SG only — belt-and-suspenders). `deletion_protection=true`.
- **Cloudflare Access**: sits in front of the ALB, intercepting *every* path (including `/health` and `/debug/*`) with a redirect to its login page — this is not something in the Terraform reviewed but is load-bearing for deploy-time health checks (see below).
- **SNS** (`sns.tf`): a topic that CloudWatch alarms (created by `MonitorGenerationAgent` or manually) publish to; SNS POSTs each notification to `/webhooks/cloudwatch-alarm`, with `endpoint_auto_confirms = true` matching the FastAPI handler's auto-confirm behavior. Comment: "production has zero polling load — AWS does the watching" (note: the actual `Notification` handling is currently a no-op — see §9, this design intent isn't fully realized in code).
- **GitHub OIDC** (`github_oidc.tf`): the deploy workflow assumes `agent-platform-prod-github-deploy` via short-lived OIDC tokens scoped to `repo:sidreddy88/agent-platform:ref:refs/heads/main` — no long-lived AWS keys in GitHub secrets. The role is scoped to ECR push/pull, `ecs:UpdateService`/`DescribeServices`, and `PassRole` on the task roles only — notably **not** granted `elasticloadbalancing:DescribeLoadBalancers` (relevant to the ALB DNS bug below).
- **Security groups**: strictly chained — internet→ALB (80/443 only), ALB→ECS task (container port only, from the ALB SG), ECS task→RDS (5432 only, from the task SG); egress is wide open everywhere so the task can reach Anthropic/OpenAI/GitHub/Atlas without per-destination ACLs.

### Docker build
Multi-stage: a `node:20-alpine` frontend stage builds the React/Vite dashboard, a `python:3.13-slim` runtime stage serves it as static files via uvicorn. The runtime image also keeps `git`+`node18`+`npm` installed because `SandboxService` runs the target app's Jest suite via direct `npm test` in prod — "Docker-in-Fargate isn't viable, so the npm fallback path is the one actually used in prod" (comment in the Dockerfile).

### Deploy pipeline (`.github/workflows/deploy.yml`)
Triggers on push to `main` (path-ignoring docs/infra) or manual dispatch. Steps: OIDC auth → ECR login → Buildx build+push (ARM64, GHA layer cache) → `aws ecs update-service --force-new-deployment` → `aws ecs wait services-stable` → health check → **code graph rebuild** (`POST /debug/code-graph/index`, non-blocking — a failure here doesn't fail the deploy, since the agents fall back to RAG search when the graph is empty).

**The health-check and code-graph steps route around Cloudflare Access on purpose**: `curl --connect-to "app.remediatelabs.io:443:${ALB_DNS}:443" https://app.remediatelabs.io/health` keeps the TLS SNI/cert matching the public hostname but routes the TCP connection straight to the ALB, bypassing Cloudflare's login-page redirect entirely. `ALB_DNS` is hardcoded as a workflow env var rather than looked up dynamically.

### Recent bug history (from `git log`, most relevant to the deploy story)
This sequence, all from the same evening (2026-07-26), is a good case study in how deploy-time verification silently lied for a while:
1. **`#144` `fix(deploy): bypass Cloudflare Access for deploy-time health/index checks`** — root cause: Cloudflare Access intercepts *every* path with a 302→login page, and `curl -fL` treated that login page's 200 as a passing health check. Both the health check and the code-graph rebuild curl had been false positives — the rebuild curl never reached the app at all (0 rows in `code_graph_edges`, no log line). Fixed via `--connect-to` to route around Cloudflare while keeping the SNI/cert correct.
2. **`#146` `fix(deploy): hardcode ALB DNS instead of describe-load-balancers call`** — the fix in #144 introduced a "Resolve ALB DNS" step using `aws elbv2 describe-load-balancers`, which the deploy IAM role isn't granted (`AccessDenied`) — rather than widen the role's IAM policy for a one-line lookup, the ALB's DNS name (stable for its lifetime) was hardcoded as a workflow env var instead.
3. **`#147` `fix(code-graph): add missing tree-sitter deps to requirements.txt`** — `tree-sitter`/`tree-sitter-javascript`/`tree-sitter-typescript` were imported by `code_graph/parser.py` but never added to `requirements.txt`; they only worked in local dev "by accident" (whatever happened to be in the venv). In prod, `build_from_directory()` caught the resulting `ImportError` and silently returned an empty graph — `/debug/code-graph/index` reported success and `find_callers` had been silently falling back to search this entire time, with no visible error anywhere.
4. **`#143` `fix(docker): build frontend stage natively, not under arm64 emulation`** — the previous two deploys (#141, #142) had silently never shipped: `npm ci` in the frontend-build Docker stage hung indefinitely under QEMU ARM64 emulation and was killed by the job's 30-minute timeout. Fixed by building that stage with `--platform=$BUILDPLATFORM` (native) since it only produces static JS/CSS with no architecture dependency.
5. **`#140` `fix(repo): use x-access-token format for GitHub clone URL in non-TTY envs`** — `LocalRepoService`'s git clone URL needed the `x-access-token:{token}@github.com/...` format specifically for non-interactive (CI/container) environments.

The throughline across all five: verification steps (health checks, index rebuilds, dependency installs) had been silently "succeeding" while doing nothing, for multiple deploy cycles, before each was caught and fixed — a recurring theme worth being upfront about in interviews.

---

## 9. Known gaps / fragile spots

Being direct about weaknesses, verified against the actual code:

- **`pending_event_store` is in-memory only** (`app/services/pending_events.py:186-190` — plain `dict`/`set`, no DB backing at all). A server restart while events are sitting in the human-approval queue silently loses them, with no recovery path — unlike `IncidentStore`/`ApprovalService`, which are DB-backed and survive restarts. `event_queue.py`'s `asyncio.Queue` has the same in-memory-only property for anything already approved but not yet dequeued.
- **Tool-call errors are swallowed and never surface to CloudWatch/structured logs.** `BaseAgent._execute_tool()` (`app/agents/base.py:361-384`) catches any exception from a tool call and returns it as plain text fed back to the LLM as an `Observation`:
  ```python
  except Exception as e:
      return f"Error running tool '{name}': {e}"
  ```
  There is no `logger.error(...)` call anywhere in that path — the only record of a tool failure is inside the LLM's own conversation history for that run. If the model doesn't surface the failure in its final answer (or the run itself later succeeds via a different path), the failure leaves no trace in logs, metrics, or Langfuse spans beyond the generic tool-span output truncated to 500 chars. Across `app/agents/` and `app/services/`, there are **161 occurrences of `except Exception`** total; many of the ones in services (e.g. `incident_store.py`, `alerting.py`) do log a warning first, but the pattern in `base.py`'s core tool-execution path — the one every agent's every tool call goes through — does not.
- **SNS push ingestion is currently a no-op.** `POST /webhooks/cloudwatch-alarm`'s `Notification` handler (`app/api/routes/webhooks.py:301-311`) returns `{"status": "ack_noop"}` without doing anything — the code comment explains that collapsing every alarm transition into one generic card was "low-signal for fix-agents" since alarm payloads don't carry the matching log lines, and that real ingestion instead comes from `DetectionService`'s 5-minute log-polling loop. The `_alarm_payload_to_event()` normalizer and the whole SNS subscription-confirmation flow are still wired up and functional, but dead weight for the actual detection path — this contradicts the "push-based, zero polling load" framing used elsewhere in the same file's docstring and in `infra/agent_platform/sns.tf`'s comments.
- **Near-duplicate route handlers.** `incidents.py` has four scan endpoints (`/scan`, `/scan/14days`, `/scan/6weeks` sharing `_run_scan(days)`, plus `/scan/crashes` which duplicates most of that logic inline with a category filter instead of parameterizing) — a single `POST /incidents/scan?days=N&crashes_only=bool` would collapse all four. There are also three unrelated "approve" code paths with no shared naming convention: `POST /approvals/{id}/approve` (goes through `ApprovalService`), `POST /events/{id}/approve` (goes through `pending_event_store` + `event_queue`, bypassing `ApprovalService` entirely), and `POST /incidents/{id}/approve-fix` — a newcomer to the codebase has to learn three different approval mechanisms with three different persistence guarantees. Similarly, `DELETE /incidents`, `DELETE /incidents/events`, and `POST /events/approve-all` all "clear" state in overlapping-but-distinct ways.
- **`PerformanceAgent`'s route is dead.** `app/api/routes/performance.py` is fully implemented (`GET /performance/heaviest`) but both its import and `include_router` call are commented out in `main.py` ("disabled — Atlas-backed"), making the agent unreachable via HTTP even though the agent class itself works standalone.
- **`IncidentResponseAgent` looks like a parallel, mostly-unused design.** It implements a full risk-rated action-approval loop that closely resembles what `IncidentLoop` + `TriageAgent` + `DiagnosisAgent` now do in production, but nothing in `incident_loop.py`, the route files, or the webhook handlers calls it — it appears to be an earlier architecture that the pipeline superseded without being removed. Its hardcoded `_KNOWN_INCIDENTS` list is explicitly commented as "placeholder — replace with real DB / vector search," which the RAG-backed `DiagnosisAgent` has since actually done, in a different file.
- **The call-graph index requires a manual restart to take effect.** `CodeGraph.load_from_store()` is only called once, at module import time, in both `diagnosis.py` and `fix_generation.py`. Triggering `POST /debug/code-graph/index` rebuilds and persists edges to Postgres correctly, but the running process's in-memory graph doesn't pick up the change until it restarts — the endpoint's own response text says as much (`"Restart server to load into memory."`), so this is a documented rather than hidden limitation, but it's still an operational trap (a rebuild silently doesn't help `find_callers` for the rest of that process's life).
- **Two independent, differently-configured blast-radius limits.** `BlastRadiusGuard` (`app/services/blast_radius.py`) enforces `max_files=5`/`max_lines_added=500`/`max_lines_deleted=500` and is checked *before* any GitHub write. `DefinitionOfDoneChecker.blast_radius_respected` (`app/services/dod_checker.py`) separately hardcodes a 5-file limit and re-checks *after* the PR already exists, falling back to a live GitHub diff fetch if `pr_files_changed` is empty. They happen to agree on file count today, but are two separate, unlinked constants that could silently drift apart.
- **`SandboxService`'s compatibility patches are brittle.** It applies two hardcoded string-literal replacements (a `config/index.js` NODE_ENV allowlist tweak and a `server.js` `app.listen` guard) to make the cloned target repo testable in isolation. If the upstream source drifts from the exact expected text, the patch silently no-ops (logs a warning) rather than failing loudly — a sandbox validation could pass against effectively-unpatched code without anyone noticing.
- **4KB atomic-write assumption in JSONL append paths.** Both `golden_dataset_builder.py` and `preference_logger.py` rely on POSIX `PIPE_BUF`-sized (~4KB) writes being atomic to avoid interleaved/corrupted lines under concurrent writers; a large `full_trace` payload pushing a single JSON line over that size could corrupt the file under concurrent access. Neither uses a file lock.
- **Cooldown state for alerting resets on restart.** `threshold_monitor.py`'s per-resource 30-minute alert cooldown and `drift_detector.py`'s 24-hour alert cooldown are both plain in-memory dicts — a restart within the cooldown window can cause a duplicate alert for an ongoing issue.
- **CORS is wide open** (`allow_origins=["*"]`, `allow_methods=["*"]`, `allow_headers=["*"]` in `main.py`) — acceptable for a solo-operator demo behind Cloudflare Access, but worth flagging as something that wouldn't survive a real security review.
- **What's *not* actually a gap, despite initial appearances**: the repo has a real, substantial `tests/` suite — 34 test files (not the near-empty suite a first pass might suggest), covering agents (triage/CICD/code-review/requirements), core services (circuit breaker, checkpoint, drift detector, eval runner, golden dataset builder, LLM gateway, RAG, vector store, schema validator), and pipeline integration (`test_incident_pipeline.py`, `test_refix_from_review.py`, `test_pr_merge_poller.py`). CLAUDE.md's claim that "tests use mocks — no live API calls required" is broadly consistent with the stubbing patterns seen in `eval_runner.py` (`_EvalAWSStub`, `_EvalStoreStub`).

---

### Key file references for orientation
- Pipeline orchestration: `app/services/incident_loop.py`
- Data model: `app/models/events.py`
- Agent framework: `app/agents/base.py`
- Diagnosis grounding logic: `app/agents/diagnosis.py:725-867`
- Fix generation + tiered context + self-critique: `app/agents/fix_generation.py`
- RAG hybrid search: `app/services/rag.py`
- Call graph: `app/services/code_graph/{graph,parser,store}.py`
- Route wiring: `app/main.py`
- Webhook ingestion: `app/api/routes/webhooks.py`
- Deploy pipeline: `.github/workflows/deploy.yml`
- Infra: `infra/agent_platform/*.tf`
