# Design Decisions

Architectural choices that are not derivable from the code alone. Read before making
decisions that touch the same areas — avoid re-litigating settled questions.

---

## 2026-04-25: SQLite over a heavier database

**Decision:** SQLite with WAL mode for all persistence (`agent_platform.db`).

**Reason:** The platform runs as a single process. All reads and writes are local. SQLite
with WAL mode handles concurrent reads well and is zero-infrastructure — no separate
database process to run or configure.

**Rejected:** PostgreSQL. Adds operational overhead (separate process, connection pooling,
migration tooling) with no benefit at current scale. Revisit if the platform ever runs
multiple workers or needs cross-host access.

**Constraint:** All schema changes must use `ALTER TABLE ... ADD COLUMN` wrapped in
`try/except` — no `DROP` or rename operations, as values are stored in JSON blobs.

---

## 2026-04-25: Stack trace-only file resolution in fix generation

**Decision:** `FixGenerationAgent` resolves the target file exclusively from stack traces.
No fallback to code search.

**Reason:** A real incident: the error type string appeared in a comment in `ecsHelper.js`.
Code search returned that file as the top result. The agent generated a syntactically
valid fix for the wrong file, which passed all downstream checks (blast radius, syntax)
and was nearly merged.

**Rejected:** Code search by error type string as a fallback strategy.

**Constraint:** If no file can be resolved from the stack trace, fix generation is skipped
entirely. No fix is better than a fix to the wrong file.

---

## 2026-04-25: PR_BASE = "staging", not "main"

**Decision:** All fix branches fork from `staging`; PRs target `staging`.

**Reason:** When branches forked from `main` while PRs targeted `staging`, every PR diff
showed all commits in `main` not yet merged to `staging` alongside the actual fix commit.
Reviewers could not identify which change was the fix.

**Rejected:** Forking from `main`.

**Constraint:** `get_branch_sha(PR_BASE)` must be used for branch creation, and
`get_file_contents(ref=PR_BASE)` for reading the file before generating the diff.

---

## 2026-04-26: ruff only, mypy deferred

**Decision:** `make lint` runs ruff only. mypy is not configured.

**Reason:** Running mypy for the first time surfaced 50+ pre-existing type annotation
errors across the codebase. Adding mypy to `make check` would permanently break the
feedback gate, defeating its purpose as a binary pass/fail signal.

**Rejected:** Adding mypy to `make lint` immediately.

**Constraint:** mypy should be added incrementally once annotations are added. Do not
add mypy to `Makefile` without first resolving or suppressing the pre-existing errors.

---

## 2026-04-26: `fix_with_steps` is the public API for FixGenerationAgent

**Decision:** Code that calls `FixGenerationAgent` calls `fix_with_steps(incident)`,
not `fix(incident)`. Tests mock `fix_with_steps`.

**Reason:** `fix_with_steps` returns `tuple[FixResult, list[str]]` — the steps list
is needed for logging and the approve-fix endpoint. `fix()` is a thin wrapper that
discards steps. `incident_loop._run_fix()` calls `fix_with_steps`, not `fix`.

**Rejected:** Mocking `fix()` in tests. This silently fails — the mock is never applied
because `_run_fix` calls a different method.

**Constraint:** Always mock `fix_with_steps`, not `fix`, in tests.

---

## 2026-04-26: AGENTS.md as 80-line routing file

**Decision:** AGENTS.md is kept at ~80 lines and functions as a router, not a container.
Detailed contracts live in topic docs (`CONSTRAINTS.md`, `ARCHITECTURE.md` files).

**Reason:** At 284 lines, AGENTS.md consumed 4,000–6,000 tokens before any task work
began. Critical rules buried in the middle were being ignored (lost-in-the-middle effect).
No priority distinction between hard constraints and historical notes.

**Rejected:** Continuing to add rules to AGENTS.md as incidents occurred.

**Constraint:** New rules go into the topic doc that owns their domain, not into AGENTS.md.
If a rule cannot be assigned to a topic doc, it goes into CONSTRAINTS.md. AGENTS.md
stays at ~80 lines.

---

## 2026-04-26: Haiku for TriageAgent, Sonnet for all others

**Decision:** `TriageAgent` uses `claude-haiku-4-5`. All other agents use `claude-sonnet-4-6`.

**Reason:** Triage is high-volume (every error event goes through it) and low-complexity
(real / noise / duplicate is a 3-way classification). Haiku is ~10× cheaper and fast
enough for this task. Downstream agents (diagnosis, fix generation, code review) require
reasoning depth that justifies Sonnet.

**Rejected:** Using Sonnet for all agents (cost), using Haiku for all agents (quality).

**Constraint:** If TriageAgent accuracy degrades, upgrade to Sonnet before adding prompt
complexity.

---

## 2026-04-26: `_apply_dod_gate` as module-level function, not method

**Decision:** The Definition of Done gate is a module-level async function in
`incident_loop.py`, not a method on `IncidentLoop`.

**Reason:** The gate is called from three locations: `_process()`, `resume_fix()`, and
the `approve-fix` API endpoint (`app/api/routes/incidents.py`). Making it a method would
require the route handler to import `incident_loop` (the singleton), which works but
creates a tighter coupling than a standalone function.

**Rejected:** Instance method on `IncidentLoop`.

**Constraint:** All three call sites must call `_apply_dod_gate` and follow the same
ordering: register PR → run gate → set REVIEWING only if gate returns True.
