# Project Progress

Session handoff file. Update "Current State" and "Next Steps" at the start and end
of each working session.

---

## Current State

- **Branch:** main
- **Latest commit:** `ce1e36e` — chore(scrub): remove brand/product-name terms from tracked content (#213)
- **Tests:** 871 pass, 3 deselected (live-API tests, opt-in only), 3 pre-existing
  failures (see Known Issues)
- **Lint:** clean (`make lint` passes)

---

## Getting Started (new session)

Before doing anything else, confirm:

- `make setup` completes without error
- `make check` passes (lint + tests, 3 known failures expected — see Known Issues)
- "Current State" above is accurate (branch, commit, test count)
- "Next Steps" below has a concrete first action

---

## Before You Stop

- `make check` passes (lint + tests, 3 known failures expected — no new failures)
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
- Stack trace-only file resolution, with GitHub Code Search and RAG as fallback layers

### Diagnosis Grounding
- `submit_diagnosis` tool is the only way `DiagnosisAgent` finalizes — every structural
  check (function/file existence, file↔function pairing, snippet verbatim-matching)
  runs inline before acceptance, not as a post-hoc audit (see DECISIONS.md)
- `diagnosis_grounding_rejections` tracks how often the gate actually fires;
  `scripts/measure_diagnosis_grounding.py` reports the rate

### Engineering Docs & Tooling
- `AGENTS.md` — routing file, ~90 lines, points to topic docs
- `CONSTRAINTS.md` — hard MUST/MUST NOT rules
- `app/agents/ARCHITECTURE.md` / `app/services/ARCHITECTURE.md` — per-area architecture notes
- `docs/ARCHITECTURE.md` — full repo architecture reference, verified against source
- `DECISIONS.md` — architectural decision log
- `.python-version`, `.nvmrc`, `pyproject.toml` (ruff), `Makefile` (`make check`)

### Frontend
- Incident table with status badges and age
- Event approval flow (pending events queue)
- Archive and mark-wrong-fix actions
- Circuit breaker status UI
- Manual "Try Error Clarity" trigger + approve-with-notes for diagnosis escalations

### LLM Gateway
- `LLMGateway` — config-driven routing via `config/llm_routing.json`; all agents route through it
- `LiteLLMProvider` — single universal adapter replacing separate Anthropic/OpenAI SDK clients
- Cross-provider diversity guard: fix and review must resolve to different providers
  (currently Anthropic Sonnet for fix, `openai/gpt-4.1` for review)
- Model IDs centralized in `app/services/model_config.py` — one place to update
  when a provider retires a snapshot, after a retired-snapshot incident broke this silently
- Cost accumulation by task type and provider; exposed via `/metrics`
- Langfuse generation spans wired into every LLM call

### Infrastructure
- Postgres/SQLite dual-backend persistence via `app/services/database.py` (dialect-aware)
- ChromaDB (local) / pgvector (Postgres) for RAG, backend picked automatically
- Langfuse v4 tracing — silently disabled when keys absent
- Dedup: SQL exact-match → regression check → RAG similarity
- Target harness content (`targets/target-app/`) fetched from a private S3 bucket at
  container startup — decoupled from code deploys (see DECISIONS.md)

---

## In Progress

Nothing currently active.

---

## Known Issues

Pre-existing test failures — do not fix by removing assertions:
- `tests/test_drift_detector.py::TestCurrentDriftFloor` (2 tests)
- `tests/test_drift_detector.py::TestCurrentDriftBaseline::test_no_drift_when_rates_similar`

Root cause: time-window-dependent assertions in the drift detector's rolling-baseline
comparison — not a real logic bug, just brittle against real wall-clock time.

---

## Next Steps

_PROGRESS.md is the single source of truth for task state. Do not maintain a parallel
list elsewhere._

1. See `FUTURE.md` for deferred features and their pre-conditions.
2. Fix the 3 pre-existing drift-detector test failures `[not_started]`
   _Done when:_ `make test` shows 0 failures.
