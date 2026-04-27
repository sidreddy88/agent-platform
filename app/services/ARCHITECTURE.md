# app/services — Architecture

## Module-Level Singletons

Every service file exports a singleton at module level. Import the singleton, never
instantiate the class directly in application code.

**Core pipeline** (used by incident_loop and orchestrator):
| Singleton | Class | File |
|---|---|---|
| `event_queue` | `EventQueue` | `event_queue.py` |
| `orchestrator` | `MasterOrchestrator` | `orchestrator.py` |
| `incident_loop` | `IncidentLoop` | `incident_loop.py` |
| `incident_store` | `IncidentStore` | `incident_store.py` |
| `dod_checker` | `DefinitionOfDoneChecker` | `dod_checker.py` |
| `approval_service` | `ApprovalService` | `approvals.py` |
| `pending_event_store` | `PendingEventStore` | `pending_events.py` |

**Fix pipeline**:
| Singleton | File |
|---|---|
| `blast_radius_guard` | `blast_radius.py` |
| `handoff_validator` | `schema_validator.py` |
| `preference_logger` | `preference_logger.py` |

**Infrastructure**:
| Singleton | File |
|---|---|
| `circuit_breaker_registry` | `circuit_breaker.py` |
| `context_checkpointer` | `checkpoint.py` |
| `agent_tracker` | `agent_tracker.py` |
| `alerting_service` | `alerting.py` |

**Monitoring** (used by background tasks, not incident pipeline directly):
`drift_detector`, `threshold_monitor`, `detection_service`, `latency_tracker`

---

## Pipeline Service Graph

```
ErrorEvent
  → event_queue
      → orchestrator          (routes by source/type)
          → incident_loop._process()
              → incident_store.create()
              → TriageAgent
              → DiagnosisAgent
              → FixGenerationAgent  → blast_radius_guard, handoff_validator
              → _apply_dod_gate()   → dod_checker, incident_store
              → CodeReviewAgent
              → approval_service
              → alerting_service
              → preference_logger   (on rejection)
```

**`incident_store`** is a dependency of almost everything — it holds all incident state and
is read/written at every pipeline stage.

---

## `_apply_dod_gate` — Three Call Sites

`_apply_dod_gate` is a **module-level async function** in `incident_loop.py`, not a method.
It must be called at every `REVIEWING` transition:

1. `incident_loop.py:_process()` — main happy path after PR creation
2. `incident_loop.py:resume_fix()` — after diagnosis-escalation approval resumes the fix
3. `app/api/routes/incidents.py:approve_fix` — after pending diff is committed

All three must follow this order:
```python
incident_store.set_pr_for_resource(key, fix.pr_url)   # register BEFORE gate
if not await _apply_dod_gate(incident):
    return                                              # gate sets VERIFICATION_FAILED
incident.status = IncidentStatus.REVIEWING
incident_store.update(incident)
```

---

## `incident_id_ctx` — ContextVar Scoping

`incident_id_ctx` is a `ContextVar[str | None]` defined in `app/agents/base.py`. It links
every agent run to its incident in `agent_tracker`.

**Rule:** call `incident_id_ctx.set(incident.id)` at the start of every pipeline task that
creates agent runs. In `_process()`, this is set once and all awaited agents inherit it via
Python's `asyncio` context propagation. If you add a new pipeline entry point that runs
agents, set this variable.

---

## Circular Import Protection

`dod_checker.py` imports `incident_store` lazily (inside `check_monitor_pr_map_updated`)
to avoid a circular import: `incident_loop → dod_checker → incident_store → incident_loop`.

`incident_loop.py` uses `TYPE_CHECKING` guards for type annotations that would otherwise
create cycles.

**Rule:** when adding a new cross-service import, check whether it creates a cycle. If it
does, use a local import inside the function body or a `TYPE_CHECKING` guard.

---

## Testing Singletons

Because singletons are module-level, mock them at the **import site** — not via the class:

```python
# correct — patches the name as incident_loop sees it
with patch("app.services.incident_loop.incident_store") as mock_store:
    ...

# wrong — patches the class but incident_loop already holds a reference to the singleton
with patch("app.services.incident_store.IncidentStore") as mock_cls:
    ...
```

`GitHubService` is not a singleton — it is instantiated per-use. Patch at the import site:
```python
patch("app.services.incident_loop.GitHubService")
```

---

## SQLite (`database.py`)

WAL mode enabled. Tables: `incidents`, `approvals`, `agent_runs`, `monitor_pr_map`,
`monitor_records`.

Adding a column: use `ALTER TABLE ... ADD COLUMN` wrapped in `try/except` (SQLite has no
`IF NOT EXISTS` for columns). Never `DROP` or rename columns — values are stored in JSON
blobs and read back by Pydantic field name, not column name.

---

## Preference Logger — Harness Failure Layers

`preference_logger.py` writes JSONL to `.preference_pairs.jsonl`. One record per human
rejection. The `harness_failure_layer` field classifies which pipeline layer caused the
bad fix:

| Value | Meaning |
|---|---|
| `task_specification` | Agent misunderstood what to fix |
| `context_provision` | Wrong file fetched, missing callers/imports |
| `execution_environment` | GitHub 404, CloudWatch unavailable, tool failure |
| `verification_feedback` | Fix not verified before PR, test missing |
| `state_management` | Agent lost incident context mid-run |
| `model_capability` | Genuine model failure; no harness change would have caught it |

Field defaults to `None`. Set explicitly at the call site in `app/api/routes/approvals.py`.
Filter `execution_environment` records out of RLHF training data — they are not model failures.
