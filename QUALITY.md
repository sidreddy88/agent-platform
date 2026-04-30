# Quality Document

Active tracker for module health. Update grades as quality changes — not a one-time
snapshot but an ongoing record of whether the codebase is getting stronger or weaker.

New sessions: read this to prioritize. Fix the lowest-scoring module first.

---

## Dimensions

| Dimension | A | B | C | D |
|---|---|---|---|---|
| Verification | All tests pass | Pre-existing gaps | Partial pass | Build fails |
| Understandability | Clear, self-contained | Some complexity | Difficult to follow | Requires deep context |
| Test stability | Stable, no flakes | Minor gaps | Unstable / flaky | No meaningful coverage |
| Architecture boundaries | Fully compliant | Minor deviations | Notable violations | Serious violations |
| Code conventions | Followed | Mostly followed | Inconsistent | Not followed |

---

## Fix Generation — Grade: B

**File:** `app/agents/fix_generation.py`

| Dimension | Status |
|---|---|
| Verification | Partial — 2 pre-existing test failures in `tests/test_blast_radius.py` |
| Understandability | Difficult — multi-strategy `_resolve_target`, call chain context, self-critique all in one file |
| Test stability | Unstable — `test_protected_file_path_blocks_pr_creation`, `test_safe_fix_passes_blast_radius` fail |
| Architecture boundaries | Compliant — CONSTRAINTS.md rules enforced (stack trace-only, symptom-fix rejection) |
| Code conventions | Followed |

**Known issues:**
- `tests/test_blast_radius.py::TestFixGenerationBlastRadius` (2 tests) — LLM mock responses don't trigger blast radius evaluation
- Self-critique pass is advisory (non-blocking) — LOOKS CORRECT verdict doesn't prevent a symptom fix from getting through if RAG misses the root cause file

**Next improvement:** Fix blast radius test mocks so they trigger the evaluation path.

---

## Incident Pipeline — Grade: B

**Files:** `app/services/incident_loop.py`, `app/services/orchestrator.py`

| Dimension | Status |
|---|---|
| Verification | Partial — 2 pre-existing test failures in `tests/test_orchestrator.py` |
| Understandability | Difficult — complex state machine with 11 statuses, DoD gate, dedup map, circuit breaker wiring |
| Test stability | Unstable — `test_cloudwatch_incident_fires_enrichment`, `test_stats_incremented_correctly` fail |
| Architecture boundaries | Compliant — DoD gate at all 3 required call sites, PR registered before gate |
| Code conventions | Followed |

**Known issues:**
- `enrichment_fired` stat counter not incrementing in tests
- `cloudwatch` dispatch path not triggering enrichment in mock environment
- Dedup key inconsistency: `_process()` uses composite key, `resume_fix()` uses error_type only (documented in CONSTRAINTS.md — do not normalize without updating all tests)

**Next improvement:** Fix stat counter mock so orchestrator tests pass.

---

## Agent Core — Grade: A

**Files:** `app/agents/base.py`, `app/agents/triage.py`, `app/agents/diagnosis.py`, `app/agents/code_review.py`, `app/agents/cicd.py`, `app/agents/deployment.py`, `app/agents/performance.py`

| Dimension | Status |
|---|---|
| Verification | All tests pass |
| Understandability | Clear — ReAct loop in base.py is well-documented; each agent is focused |
| Test stability | Stable |
| Architecture boundaries | Compliant — MUST NOT MODIFY base.py constraint respected |
| Code conventions | Followed |

**Constraints:** `base.py` is frozen per CONSTRAINTS.md — changes affect every agent.

---

## Services Layer — Grade: A

**Files:** `app/services/` (excluding `incident_loop.py`, `orchestrator.py`)

Key files: `github.py`, `rag.py`, `agent_tracker.py`, `latency.py`, `approvals.py`, `circuit_breaker.py`, `tracing.py`, `blast_radius.py`, `dod_checker.py`

| Dimension | Status |
|---|---|
| Verification | All tests pass |
| Understandability | Clear — each service is single-responsibility |
| Test stability | Stable |
| Architecture boundaries | Compliant — circuit breaker wrapping all external calls |
| Code conventions | Followed |

---

## API Routes — Grade: A

**Files:** `app/api/routes/`

| Dimension | Status |
|---|---|
| Verification | All tests pass |
| Understandability | Clear — one file per domain |
| Test stability | Stable |
| Architecture boundaries | Compliant |
| Code conventions | Followed |

---

## Frontend — Grade: A

**Directory:** `frontend/`

| Dimension | Status |
|---|---|
| Verification | No known failures |
| Understandability | Clear — React + TypeScript, one component per feature |
| Test stability | N/A (no frontend unit tests) |
| Architecture boundaries | Compliant — types kept in sync with backend enums per CONSTRAINTS.md |
| Code conventions | Followed |

**Note:** No frontend unit tests. Manual testing only. A dedicated frontend test suite would move this to full A.

---

## Harness — Grade: A

**Files:** `AGENTS.md`, `PROGRESS.md`, `CONSTRAINTS.md`, `DECISIONS.md`, `FUTURE.md`, `QUALITY.md`, `Makefile`

| Dimension | Status |
|---|---|
| Verification | `make check` wires lint + test + arch-check |
| Understandability | Clear — each file has a defined scope and is referenced in AGENTS.md Topic Docs |
| Test stability | Stable |
| Architecture boundaries | Enforced — Review Feedback Promotion process in CONSTRAINTS.md |
| Code conventions | Followed |

**Harness completeness:** L5–L12 series implemented — decision log, bootstrap contract, WIP limit, feature list triple, termination check, arch boundary enforcement, sprint contracts, exit checklist.
