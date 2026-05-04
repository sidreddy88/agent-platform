# Project Progress

Session handoff file. Update "Current State" and "Next Steps" at the start and end
of each working session.

---

## Current State

- **Branch:** main
- **Latest commit:** `7a58bc0` — Merge pull request #80 from sidreddy88/feat/llm-gateway
- **Tests:** 621 pass, 9 pre-existing failures (see Known Issues)
- **Lint:** clean (`make lint` passes)

---

## Bootstrap Contract

All four conditions must be true for a clean session handoff.

- `make setup` completes without error
- `make check` passes (lint + tests, 4 known failures expected)
- "Current State" above is accurate (branch, commit, test count)
- "Next Steps" below has a concrete first action

---

## Session Exit Checklist

All five must be true before a session is considered complete.

- `make check` passes (lint + tests, 4 known failures expected — no new failures)
- PROGRESS.md "Current State" updated (branch, commit hash, test count)
- "Next Steps" has a concrete first action for the next session
- No debug code, temporary print statements, or scratch files left in modified files
- Active branch either has a PR open or its next step is recorded in In Progress

---

## Completed

### Incident Pipeline
- `ErrorEvent → EventQueue → MasterOrchestrator → IncidentLoop`
- Stages: Triage → Diagnosis → Confidence gate → Fix → DoD Gate → Reviewing → Approval
- `IncidentStatus` enum with 11 values including `VERIFICATION_FAILED`
- Circuit breaker wrapping all external service calls

### Fix Quality
- Call chain context: fetches imports and callers before generating a fix
- Self-critique pass (Haiku) after fix generation, before PR creation
- Anti-symptom rules in fix prompt (exception suppression, wrong-layer sanitization, etc.)
- Stack trace-only file resolution — no fallback code search

### Harness
- `harness_failure_layer` attribution on rejection JSONL records (6 values)
- Definition of Done gate: 5 checks before every `REVIEWING` transition
- `AGENTS.md` trimmed to 85-line routing file with Topic Docs table
- `CONSTRAINTS.md` — hard MUST/MUST NOT rules
- `app/agents/ARCHITECTURE.md` — ReAct loop contracts, tool registration, fix gen rules
- `app/services/ARCHITECTURE.md` — singleton inventory, pipeline graph, DoD gate wiring
- `DECISIONS.md` — architectural decision log
- `.python-version`, `.nvmrc`, `pyproject.toml` (ruff), `Makefile` (`make check`)

### Frontend
- Incident table with status badges and age
- Event approval flow (pending events queue)
- Archive and mark-wrong-fix actions
- Circuit breaker status UI

### LLM Gateway (PR #80)
- `LLMGateway` — config-driven routing via `config/llm_routing.json`; all agents route through it
- `LiteLLMProvider` — single universal adapter replacing separate Anthropic/OpenAI SDK clients
- `GatewayLLMService` — duck-typed drop-in for `LLMService`; injected into all pipeline agents
- fix=`claude-sonnet-4-6`, review=`gpt-4.5`; hard validation at init prevents same-provider config
- Cost accumulation by task type and provider; exposed via `/metrics`
- Langfuse generation spans wired into every LLM call

### Infrastructure
- SQLite persistence (`agent_platform.db`) — WAL mode, 5 tables
- ChromaDB vector store for RAG (OpenAI `text-embedding-3-small`)
- Langfuse v4 tracing — silently disabled when keys absent
- Dedup: SQL exact-match → regression check → RAG similarity

---

## In Progress

### Pending Event Rescan Refresh

- **Scope:** `app/services/pending_events.py`, `app/api/routes/incidents.py`, `frontend/src/hooks/useWebSocket.ts`, `frontend/src/App.tsx`, `frontend/src/components/IncidentsPage.tsx`
- **Exclusions:** incident pipeline triage/diagnosis/fix behavior, AWS log fetch implementation
- **Done when:** clicking `Clear Events` reconciles the UI to zero pending events, and the next `Scan Last 14 Days` creates a pending event for each distinct raw log match returned by the scan instead of collapsing repeated message templates or stopping after 10 events.
- **Status:** implemented locally; `make lint` passes; `DEBUG=false pytest tests/test_pending_events.py -q` passes. Frontend typecheck blocked because `node`/`npm` are unavailable in this shell.

---

## Known Issues

Pre-existing test failures — do not fix by removing assertions:
- `tests/test_blast_radius.py::TestFixGenerationBlastRadius` (2 tests)
- `tests/test_orchestrator.py::TestPipelineDispatch::test_cloudwatch_incident_fires_enrichment`
- `tests/test_orchestrator.py::TestRouteLog::test_stats_incremented_correctly`
- `tests/test_checkpoint.py::TestBaseAgentCheckpointIntegration` (2 tests)
- `tests/test_incident_pipeline.py::TestApprovalResolution::test_approve_resolves_incident`
- `tests/test_refix_from_review.py::TestRunPostFixRequestChanges` (3 tests)
- `tests/test_schema_validator.py::TestValidateTriageViolations::test_unknown_blast_radius_raises`

Root cause: LLM mock responses don't trigger blast radius evaluation; orchestrator
`enrichment_fired` stat counter not incrementing; checkpoint and refix tests rely on
internal mocking patterns that diverged from current implementation.

---

## Next Steps

_PROGRESS.md is the single source of truth for task state. Do not maintain a parallel list elsewhere._

1. Continue harness series `[not_started]`
   _Done when:_ PR merged, `make check` passes, temp-notes.md updated with analysis

2. Add mypy incrementally `[blocked]` — pre-condition: annotations in place (see DECISIONS.md)
   _Done when:_ `mypy app/` runs clean in `make check` with zero suppressed errors

3. Fix the 4 pre-existing test failures `[not_started]`
   _Done when:_ `make test` shows 589 passing, 0 failures

4. See `FUTURE.md` for deferred features `[blocked]` — pre-condition per FUTURE.md entry
   _Done when:_ the pre-condition listed in the relevant FUTURE.md entry is met
