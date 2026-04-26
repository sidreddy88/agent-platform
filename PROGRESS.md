# Project Progress

Current state of the agent platform as of PR #28. Update this file when merging a significant PR.

## Built (on main)

### Incident Pipeline
- `ErrorEvent → EventQueue → MasterOrchestrator → IncidentLoop`
- Stages: Triage → Diagnosis → Fix → DoD Gate → Reviewing → Approval
- `IncidentStatus` enum with 11 values including `VERIFICATION_FAILED`
- Circuit breaker wrapping all external service calls

### Fix Quality
- Call chain context: fetches imports and callers before generating a fix
- Self-critique pass (Haiku) after fix generation, before PR creation
- Anti-symptom rules in fix prompt (exception suppression, wrong-layer sanitization, etc.)
- Stack trace file resolution — only strategy; no fallback code search

### Harness
- `harness_failure_layer` attribution on rejection JSONL records (6 values)
- Definition of Done gate: 5 checks run before every `REVIEWING` transition
  - `fix_test_written`, `pr_has_confidence_score`, `pr_linked_to_issue`, `monitor_pr_map_updated`, `blast_radius_respected`
- `AGENTS.md` — coding-agent harness reference (tech stack, contracts, pitfalls, verification)

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

## Not Started

- Lecture 3+ content
- mypy type checking (many pre-existing errors; deferred)
- Automated staleness recovery for `VERIFICATION_FAILED` incidents

## Known Pre-existing Test Failures (do not fix by removing assertions)

- `tests/test_blast_radius.py::TestFixGenerationBlastRadius` (2 tests) — LLM mock responses don't trigger blast radius evaluation
- `tests/test_orchestrator.py::TestPipelineDispatch::test_cloudwatch_incident_fires_enrichment`
- `tests/test_orchestrator.py::TestRouteLog::test_stats_incremented_correctly`
