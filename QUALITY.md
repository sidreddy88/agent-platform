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

## Fix Generation — Grade: A-

**File:** `app/agents/fix_generation.py`

| Dimension | Status |
|---|---|
| Verification | All tests pass |
| Understandability | Difficult — multi-strategy `_resolve_target`, call chain context, self-critique, tiered-context prompting all in one file |
| Test stability | Stable |
| Architecture boundaries | Compliant — CONSTRAINTS.md rules enforced (stack trace-only, symptom-fix rejection) |
| Code conventions | Followed |

**Known issues:**
- Self-critique pass is advisory (non-blocking) — LOOKS CORRECT verdict doesn't prevent a symptom fix from getting through if RAG misses the root cause file

**Next improvement:** The file has grown large enough (multiple resolution strategies,
tiered prompting, self-critique) that splitting `_resolve_target`'s strategies into
their own module would help understandability.

---

## Incident Pipeline — Grade: A-

**Files:** `app/services/incident_loop.py`, `app/services/orchestrator.py`

| Dimension | Status |
|---|---|
| Verification | All tests pass |
| Understandability | Difficult — complex state machine with 11 statuses, DoD gate, dedup map, circuit breaker wiring |
| Test stability | Stable |
| Architecture boundaries | Compliant — DoD gate at all 3 required call sites, PR registered before gate |
| Code conventions | Followed |

**Known issues:**
- Dedup key inconsistency: `_process()` uses composite key, `resume_fix()` uses error_type only (documented in CONSTRAINTS.md — do not normalize without updating all tests)
- Near-duplicate scan route handlers (`/scan`, `/scan/14days`, `/scan/6weeks`, `/scan/crashes`) — could collapse into one parameterized endpoint

**Next improvement:** Collapse the scan endpoints into `POST /incidents/scan?days=N&crashes_only=bool`.

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

## Engineering Docs & Process — Grade: A

**Files:** `AGENTS.md`, `PROGRESS.md`, `CONSTRAINTS.md`, `DECISIONS.md`, `FUTURE.md`, `QUALITY.md`, `Makefile`

| Dimension | Status |
|---|---|
| Verification | `make check` wires lint + test + arch-check |
| Understandability | Clear — each file has a defined scope and is referenced in AGENTS.md's Topic Docs table |
| Test stability | Stable |
| Architecture boundaries | Enforced — Review Feedback Promotion process in CONSTRAINTS.md |
| Code conventions | Followed |
