# Project Progress

Session handoff file. Update "Current State" and "Next Steps" at the start and end
of each working session.

---

## Current State

- **Branch:** main
- **Latest commit:** `65b630b` — Trim AGENTS.md to 85-line routing file; move content to topic docs
- **Tests:** 585 pass, 4 pre-existing failures (see Known Issues)
- **Lint:** clean (`make lint` passes)

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

### Infrastructure
- SQLite persistence (`agent_platform.db`) — WAL mode, 5 tables
- ChromaDB vector store for RAG (OpenAI `text-embedding-3-small`)
- Langfuse v4 tracing — silently disabled when keys absent
- Dedup: SQL exact-match → regression check → RAG similarity

---

## In Progress

_Nothing actively in progress._

<!-- Template for active work:
- [ ] Feature name (NN% — current blocker or next micro-step)
-->

---

## Known Issues

Pre-existing test failures — do not fix by removing assertions:
- `tests/test_blast_radius.py::TestFixGenerationBlastRadius::test_protected_file_path_blocks_pr_creation`
- `tests/test_blast_radius.py::TestFixGenerationBlastRadius::test_safe_fix_passes_blast_radius`
- `tests/test_orchestrator.py::TestPipelineDispatch::test_cloudwatch_incident_fires_enrichment`
- `tests/test_orchestrator.py::TestRouteLog::test_stats_incremented_correctly`

Root cause: LLM mock responses don't trigger blast radius evaluation; orchestrator
`enrichment_fired` stat counter not incrementing. Needs underlying service behavior fixed.

---

## Next Steps

1. Continue harness series (currently at session continuity / cross-session artifacts)
2. Add mypy incrementally once annotations are in place (deferred — see DECISIONS.md)
3. Fix the 4 pre-existing test failures (underlying service behavior, not assertions)
4. See `FUTURE.md` for deferred features (RAG improvements, agentic RAG, session handoff automation)
