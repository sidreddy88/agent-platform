# Hard Constraints

Rules that cause **silent failures or data corruption** if violated. Not style — invariants.

---

## Review Feedback Promotion

When a violation pattern is caught in code review, convert it into a permanent
automated check so it never recurs undetected. The harness grows stronger with
every session.

**Process:**
1. Identify the cleanest grep or lint check that catches the pattern without false positives
2. Add it to `make arch-check` in the Makefile using this error message format:
   ```
   FAIL: <what was found and where>
   WHY:  <why this is forbidden — reference this file's section>
   FIX:  <exactly what to change and where>
   ```
3. Document the pattern in the relevant section of this file
4. Every violation category captured in review becomes a permanent line of defense

**Why this matters:** A constraint written in a document relies on the agent reading it.
A constraint enforced by `make check` runs on every PR automatically.

---

## Data Model

**MUST NOT remove fields from `IncidentState`** (`app/models/events.py`)
Existing SQLite rows are deserialized with `model_validate_json`. A missing field causes a
load failure and crashes the incident store on startup.

**MUST add new `IncidentState` fields as `Optional[...] = None` or with a safe default.**
Never add a required field without a default.

**MUST NOT DROP or rename columns in SQLite tables.**
Column values are stored inside JSON blobs, not as raw columns. Renaming is harmless to the
schema but the JSON field name must match the Pydantic model field name.

---

## Incident Status Transitions

**MUST call `_apply_dod_gate(incident)` before every `REVIEWING` transition.**
There are exactly 3 call sites — all three must gate:
1. `app/services/incident_loop.py` — `_process()`
2. `app/services/incident_loop.py` — `resume_fix()`
3. `app/api/routes/incidents.py` — `approve-fix` endpoint

**MUST call `incident_store.set_pr_for_resource()` BEFORE the DoD gate, not after.**
The `monitor_pr_map_updated` DoD check reads from this table. If the PR is registered after
the gate runs, that check always fails.

**MUST update the frontend when adding a new `IncidentStatus`.**
Required changes:
- `frontend/src/types.ts` — add the new value to the union type
- Status badge component — handle the new value
- `incident_store.list_active()` — add to active set if it should appear in the active feed
- `get_open_pr_for_error()` — add to the `closed` set if it is a terminal status

**`AWAITING_REFIX_APPROVAL` — code review returned REQUEST_CHANGES, waiting for human go/no-go.**
- Entered from `_run_post_fix()` when `_extract_review_recommendation(review_text) == "REQUEST_CHANGES"`.
- `incident.human_notes` is set to the full review text at this point.
- Exit via `POST /incidents/{id}/refix` (calls `refix_from_review`) or `POST /incidents/{id}/reject-refix`.
- Not a terminal status — appears in the active feed.
- The old PR is closed best-effort at the start of `refix_from_review` before creating a new one.

**`_run_post_fix` MUST call `_extract_review_recommendation` before creating the merge approval.**
If the review is REQUEST_CHANGES, the method sets status to `AWAITING_REFIX_APPROVAL` and returns early
without creating an `approval_service` merge request.

---

## Fix Generation

**MUST mock `fix_with_steps`, not `fix`, in tests.**
`IncidentLoop._run_fix()` calls `fix_agent.fix_with_steps()`. Mocking `fix` has no effect
and silently leaves the mock unapplied.
```python
# correct
loop._fix_agent.fix_with_steps = AsyncMock(return_value=(fix_result, []))
```

**MUST NOT use error-type strings as code search queries.**
Code search by error type string (e.g. "S3_NO_SUCH_KEY", "ECS_ERROR") produces false
positives — the string appears in logs, comments, and unrelated files. Function-name
search (extracted from the error title or diagnosis) is acceptable as a fallback when
no stack trace is available, because function names are specific enough to pinpoint the
definition file.

**MUST NOT generate symptom fixes.** Four patterns to reject:
- Exception suppression (`try/except` at the crash site without fixing the cause)
- Input sanitization at the wrong layer (sanitizing output at the consumer instead of fixing the producer)
- Value coercion instead of rejection (`int(x) if str(x).isdigit() else 0`)
- Defensive null checks masking missing initialization (`if obj && obj.isReady()`)

---

## Dedup Map Key Format

**`_process()` uses composite key:** `"{error_type}:{service}:{description[:100]}"`

**`resume_fix()` uses error_type only** — pre-existing inconsistency. Do not normalize without
updating every test that looks up by key.

---

## BaseAgent / ReAct Loop

**MUST NOT modify `BaseAgent._run_loop()` or the ReAct parser** (`app/agents/base.py`).
Changes here affect every agent in the platform.

**MUST set `incident_id_ctx` before awaiting any agent inside the pipeline.**
```python
from app.agents.base import incident_id_ctx
incident_id_ctx.set(incident.id)
```
Without this, agent runs are not linked to their incident in the tracker.

**Tool functions MUST be `async`, accept `**kwargs`, and return `str`.**

---

## External Services

**`GitHubService()` raises `ValueError` at construction if `GITHUB_TOKEN` is unset.**
In tests, mock at the import site:
```python
patch("app.services.incident_loop.GitHubService")
```

**`circuit_breaker.call()` takes a coroutine, not a function reference.**
```python
# correct — pass the coroutine created by the call expression
result = await cb.call(some_service.method(arg1, arg2))
```
