---
name: Agent Platform — build state
description: Complete build status of the multi-agent FastAPI platform, all features merged to main
type: project
---

Production multi-agent incident response platform. All features merged to main, branch is clean.

**Why:** Building a production multi-agent platform with FastAPI + Anthropic SDK.

---

## Architecture

Agents extending BaseAgent (ReAct loop), all backed by SQLite (agent_platform.db).

### Agents
- TriageAgent (Haiku) — real/noise/duplicate, P0–P3 severity
- DiagnosisAgent (Sonnet) — root cause + confidence gate (≥0.70 → fix, <0.70 → escalate)
- FixGenerationAgent (Sonnet) — 3-layer file resolution: stack trace parsing → GitHub Code Search → keyword+LLM; generates fix as old/new diff, creates Issue + PR
- CodeReviewAgent (Sonnet) — codebase-aware (RAG-backed); reviews PR diff, posts inline + summary comments to GitHub
- IncidentResponseAgent (Sonnet) — deep investigation via logs, metrics, RAG, past incidents
- DeploymentAgent (paused), CICDAgent, PerformanceAgent, RequirementsAgent

### Key services
- MasterOrchestrator — routes events, per-priority semaphores, in-flight dedup; DeploymentAgent enrichment paused
- IncidentLoop — sequential pipeline with 3-layer dedup (see below)
- DetectionService — disabled on startup; on-demand via scan button only
- RAGService — ChromaDB + OpenAI embeddings; two collections: `codebase` + `incidents`
- SQLite — 5 tables: incidents, approvals, agent_runs, monitor_pr_map, monitor_records
- GitHubService — PR/file/branch ops + GitHub Code Search API (`search_code`)
- AWSService — get_error_logs fetches 20 context lines after each error for stack trace capture

---

## 3-Layer Dedup Pipeline (before triage)

1. **Layer 1 — SQL open PR** (exact): drops event if open PR exists for same error_type + service + description[:100]
2. **Layer 2 — SQL regression** (exact): finds most recent resolved incident for same error_type + service; injects past root cause + fix as `prior_context` into DiagnosisAgent. Zero API cost.
3. **Layer 3 — RAG semantic** (semantic, score ≥ 0.80): fires only when layers 1+2 find nothing; cosine similarity over all indexed past incidents regardless of wording or service. Costs one embedding call.

Incident corpus grows automatically — every terminal state (NOISE, DUPLICATE, RESOLVED, REJECTED) calls `RAGService.index_incident()`.

DiagnosisAgent accepts `prior_context` and injects it as PRIOR KNOWLEDGE in the prompt.

---

## FixGenerationAgent — File Resolution Strategy

Primary: stack trace parsing — finds first user-code frame (skips node_modules/internals), strips container prefix (`/app/`), validates path exists in repo via GitHub API.

Fallback chain:
1. GitHub Code Search API — searches file *contents* for the error type string
2. Keyword search on repo tree (server-side JS/TS only) + LLM picks from validated candidates

`_generate_fix`: passes full file content to LLM, returns `{"old": verbatim text, "new": fixed}`, validates `old` exists in actual file before committing.

AWSService `get_error_logs` now fetches 20 context lines after each matching error event (5-second window, same log stream) so stack trace frames are included in `event.description`. Description cap raised to 600 chars in scan route.

---

## Dedup keys (all layers)

- Orchestrator in-flight: `pipeline:error_type:service:description[:80]`
- Pre-triage PR gate: `error_type + service + description[:100]`
- TriageAgent check_duplicate_pr: `error_type:service:description_prefix[:100]`
- Scan normalisation: `\b\d+\b → N` before dedup sig (collapses numeric variants)

---

## Frontend (React + TypeScript, port 4000)

Tabs: Dashboard | Incidents | Agent PRs | Logs

- Incidents tab: pipeline stepper per incident, live scan log panel (WebSocket scan_progress)
- Scan button: POST /incidents/scan — broadcasts real-time progress via WebSocket
- Clear All button: DELETE /incidents — clears all incidents from store + DB
- Restart button (per card): POST /incidents/{id}/restart — resets incident to OPEN, re-queues event
- Vite proxy covers: /incidents, /approvals, /agents, /monitors, /ws/dashboard

---

## Merged PRs
- #17 — resume fix
- #18 — generic fix agent
- #19 — incidents tab + scan
- #20 — RAG layers (3-layer dedup)
- #21 — pipeline dedup stats
- #22 — RAG debug + eval script
- #23 — codebase-aware CodeReviewAgent (RAG-backed)

## In-flight
- `feat/fix-generation-improvements` — stack trace parsing, GitHub Code Search, context log fetching, restart/clear UI, timezone fix, description cap increase; pushed, awaiting PR + merge

## Current state
- Main: clean, PRs #17–#23 merged
- Background detection disabled — errors enter pipeline via scan button only
- Next: Agent Vault setup for secrets management (replacing .env)
