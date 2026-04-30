# Future Functionalities

Features deferred until there is enough real production data to justify them.
Come back to these after the incident corpus has grown and the eval script surfaces actual problems.

---

## OpenTelemetry Standardization

**When to implement:** When there is a concrete reason to move off Langfuse — e.g., existing
OTel infrastructure, multi-service distributed traces, or toolchain integration with Jaeger/Zipkin.

**What this is:** Standardize harness tracing on OpenTelemetry. Define a structured span
hierarchy: session span → task span → verification step spans. Annotate spans with
standard attributes (`harness.task`, `harness.sprint_contract.scope`, `harness.rubric.dimension`).
Pipe to an OTel-compatible backend.

**Why deferred:** Langfuse v4 already captures Agent → LLM → Tool span trees with metadata
and error context. OTel adds infrastructure complexity (collector, backend) with no concrete
benefit at current single-service scale.

**Pre-condition:** Existing OTel infrastructure in the stack, or a multi-service architecture
where distributed traces across services are needed.

---

## Architectural Boundary Linting

**When to implement:** When specific violation patterns are confirmed clean (no false
positives) and warrant automation beyond the current `make arch-check` stub.

### Import Direction Enforcement

**What this is:** A custom ruff plugin or import-linter configuration enforcing a
strict dependency direction across layers (e.g. agents may not import from api/routes,
services may not import from agents). Every violation caught on commit, not in review.

**Why deferred:** The current codebase has no explicitly declared layer graph. Adding
enforcement before the boundaries are defined would produce false positives or require
suppression comments throughout.

**Pre-condition:** Layer boundaries explicitly defined in `app/services/ARCHITECTURE.md`
or equivalent, with a dependency direction graph.

### Precise Code Search Guard for `_resolve_target`

**What this is:** An AST-level check verifying that `search_code` in
`app/agents/fix_generation.py` is only invoked from caller-context fetching, never
from the `_resolve_target` file-finding path. A simple grep would false-positive on
the legitimate caller-context use.

**Why deferred:** Requires Python AST parsing — grep alone cannot distinguish the
call sites. Feasible with a short script using the `ast` module, but the pattern
needs to stabilize before the check is written.

**Pre-condition:** `_resolve_target` interface is stable; no active refactoring of
the call chain context feature.

---

## Periodic Cleanup Loop

**When to implement:** When agent commits are enabled so cleanup can be committed atomically.

**What this is:** A weekly maintenance session that scans for accumulated entropy:
stale TODO markers, commented-out code, dead debug files, QUALITY.md grades that
have drifted from actual module health. Not emergency repair — routine operations that
prevent the entropy growth that Lehman's laws predict for continuously-changed systems.

**Why deferred:** Without agent commits, cleanup changes can't be committed at session
boundaries. Manual cleanup without commits leaves the repo in an ambiguous intermediate
state.

**Pre-condition:** Agent commits enabled. Recommended trigger: end of each sprint or
week, whichever comes first.

---

## Harness Simplification Protocol

**When to implement:** When `app/services/eval_runner.py` is wired to a benchmark task
suite with measurable pass rates.

**What this is:** A monthly review cycle to remove harness components that model
capability improvements have made unnecessary. Process: pick one Work Rule or
CONSTRAINTS.md entry, temporarily disable it, run benchmark tasks, remove permanently
if results don't degrade — restore or replace with a lighter alternative if they do.

**Why this matters:** Every harness constraint exists because the model can't reliably
do something on its own. As models improve, these assumptions become outdated. A
constraint essential today may be overhead in three months. Running the harness lighter
reduces cost, latency, and complexity.

**Why deferred:** Needs a benchmark suite to detect degradation. `eval_runner.py` is
scaffolded but not yet wired to specific tasks with measurable outcomes.

**Pre-condition:** `eval_runner.py` wired to benchmark tasks with quantifiable pass
rates, so disabling a constraint produces a measurable signal.

---

## Layer 3 End-to-End Verification

**When to implement:** When a staging environment with real (or contract-tested)
external services is available.

**Why Layers 1 and 2 are not enough:** The test suite is fully mocked — no live
API calls. Unit tests cannot catch interface mismatches between components,
state propagation errors across layers, or environment-specific failures (missing
config, service unavailability). These only surface when the full system runs.

### Automated End-to-End Tests

**What this is:** A test suite that exercises the full incident pipeline from
`POST /events` through triage → diagnosis → fix → approval, using real or
contract-tested external services (Anthropic API, GitHub, AWS).

**Pre-condition:** Staging environment with real services, or contract tests
(e.g. Pact) that verify integration boundaries without live calls.

### Independent Evaluator Agent

**What this is:** A separate agent that reviews completed work from the perspective
of a "picky" independent grader — distinct from the generating agent. The existing
Haiku self-critique pass in `FixGenerationAgent` already implements this pattern
for fix generation. A general-purpose evaluator would extend this to harness-level
tasks: verifying that a claimed "done" task actually satisfies the acceptance criterion.

**Why it's better than self-evaluation:** The same model generating and evaluating
favors being generous to itself. An independent evaluator, specifically tuned to be
critical, finds defects the generator rationalizes away.

**Pre-condition:** Layer 3 e2e tests in place so the evaluator has real execution
evidence to assess, not just code.

---

## Feature List Automation

**When to implement:** When the agent is enabled to commit code and PROGRESS.md
needs to be updated programmatically rather than manually.

### Verifier

A script that reads each Next Steps item's `Done when:` command, executes it, and
updates the state tag automatically (`[not_started]` → `[passing]`). The agent
submits a "verification request"; the verifier decides the state transition — the
agent cannot change states directly.

**Pre-condition:** Agent commits enabled.

### Scheduler

Reads PROGRESS.md, finds the next `[not_started]` item (respecting `[blocked]`
items), and sets it to `[active]`. Enforces WIP=1 automatically — blocks activation
if any item is already `[active]`.

**Pre-condition:** Agent commits enabled + verifier in place.

### Handoff Reporter

Auto-generates session summary from PROGRESS.md state distribution: how many items
are passing, active, blocked, not_started. Replaces the manual PROGRESS.md update
step at session end.

**Pre-condition:** Agent commits enabled + scheduler + verifier in place.

---

## Task Boundary Automation

**When to implement:** When agent commits are enabled and tasks are managed
programmatically rather than manually updated in PROGRESS.md.

### Scope Surface as Machine-Readable File

**What this is:** A JSON or Markdown DAG tracking all task states: `not_started`,
`active`, `blocked`, `passing`. Each node includes a task ID, description, acceptance
criterion (executable command), dependencies, and current state. A new session reads
this file and immediately knows: what is active, what counts as done, what has passed.

**Why deferred:** With 4 next steps, PROGRESS.md already serves this role with less
overhead. A structured DAG adds value when there are 10+ concurrent tasks across
features or multiple people working simultaneously.

**Pre-condition:** 10+ tasks being tracked concurrently, or multiple contributors
needing a machine-readable format.

### VCR (Verified Completion Rate) Tracking

**What this is:** Continuously track `verified tasks / activated tasks`. Block new
task activations when VCR < 1.0 — the harness enforces WIP=1 automatically rather
than relying on the Work Rules being read.

**Why deferred:** Requires programmatic task state management. Without agent commits,
there is no way to enforce the block or update task state reliably.

**Pre-condition:** Agent commits enabled + scope surface file in place.

---

## Initialization Phase Automation

**When to implement:** When the agent is enabled to commit code and the project
has enough session history to warrant measuring initialization efficiency.

### Warm Start Templates

**What this is:** A pre-seeded project template (directory structure, Makefile,
test framework, pyproject.toml, .python-version, .nvmrc) that new projects start
from rather than an empty directory. Bakes the bootstrap contract into every
new project automatically.

**Why deferred:** No new projects are being started from scratch. All current
initialization infrastructure already exists in this repo.

**Pre-condition:** Starting a new project where the team would otherwise spend
the first session on tooling setup.

### Time to First Verification Metric

**What this is:** Instrument the time from session start until the first
`make check` completion. Tracks whether the bootstrap contract is actually
achieving the target of sub-3-minute session startup.

**Why deferred:** No structured timing events in agent runs yet. Would need
the agent to emit timestamps at session boundaries.

**Pre-condition:** Agent emits structured session start/end events that can be
correlated with `make check` invocations.

---

## Per-Module PROGRESS.md

**When to implement:** When the repo grows to multiple active services with separate
owners or release cadences.

**What this is:** A PROGRESS.md co-located with each service directory (e.g.
`app/services/PROGRESS.md`, `frontend/PROGRESS.md`) tracking state at the
service level rather than the repo level.

**Why deferred:** The current architecture is a single monorepo with one active
incident pipeline. A root-level PROGRESS.md covers all in-flight work without
ambiguity. Per-module files add navigation overhead with no benefit at this scale.

**Pre-condition:** Multiple services with independent work streams, or a team
large enough that different people own different services simultaneously.

---

## ACID State Management / Git Atomicity

**When to implement:** When the agent is enabled to commit code.

**What this is:** Treating each logical unit of work as an atomic transaction —
the agent commits after each completed step, so session state is always
recoverable from git history. A new session can find the exact in-progress
state by reading the last commit message rather than relying on an in-memory
progress file.

**Why deferred:** All commits are currently manual. Automated git checkpoints
require the agent to commit code, which is not yet enabled.

**How to implement:** See the "Automated git checkpoints" section under Agent
Session Handoff below.

---

## Agent Session Handoff

**When to implement:** When the agent is enabled to commit code and update files at
session boundaries (currently all commits are manual).

**What this is:** Structured continuity artifacts so a new session can resume in ~3
minutes instead of ~15. The current architecture already has PROGRESS.md and DECISIONS.md
as the static scaffolding — this section describes the automation layer to add on top.

### Session protocol in AGENTS.md

Add a Session Protocol section once the agent commits are enabled:

```markdown
## Session Protocol

**At session start (clock-in):**
1. Read PROGRESS.md — current state and next steps
2. Read DECISIONS.md — settled architectural choices
3. Run `make check` — confirm repo is in a consistent state
4. Resume from "Next Steps" in PROGRESS.md

**At session end (clock-out):**
1. Update PROGRESS.md — current state, in-progress %, next steps
2. Record any new architectural choices in DECISIONS.md
3. Run `make check` — confirm consistent state before handing off
4. Commit all completed work with a descriptive message
```

### Automated PROGRESS.md updates

The agent should update PROGRESS.md at clock-out:
- Move completed items from "In Progress" to "Completed"
- Update "Current State" (branch, commit hash, test count, lint status)
- Update "Next Steps" with the specific next action, not just the feature name

### Automated git checkpoints

After each atomic unit of work (one logical change, tests passing):
```bash
git add <specific files>
git commit -m "feat: <what was done and why>"
```
One commit per logical unit, not one commit per session. This is the "craftsman's journal"
entry for the day — specific enough that a new session reading `git log` knows exactly
what state the work is in.

### DECISIONS.md entries written by agent

When the agent chooses between two approaches, it should write the decision before
implementing:
```markdown
## [date]: [decision title]
**Decision:** [what was chosen]
**Reason:** [why]
**Rejected:** [what was considered and dropped]
**Constraint:** [ongoing rule this creates]
```

This prevents the next session from re-evaluating a decision that was already made,
potentially choosing a different option and creating inconsistency.

### Rebuild cost target

A well-maintained session handoff should get a new session to an executable state
(read context, understand current state, run first task step) in under 3 minutes.
Measure: time from new session start to first `make check` completing.

---

## RAG Retrieval Quality Improvements

**When to revisit:** Run `python scripts/eval_rag.py` once you have 10+ real incidents indexed.
If you see near-misses (`~`) or false positives, pick up from here.

### 1. Enrich indexed text
Put diagnosis first in `index_incident` — shapes the embedding space around root cause, not incidental wording.

```
Root cause: {diagnosis} | Fix: {fix} | {title} | {error_type} | {service} | {description[:300]} | Outcome: {status}
```

File: `app/services/rag.py:index_incident`

### 2. Separate signal embedding from context text
Embed a tight signal string; store the full context as the returned document.

- **signal_text** (embedded): `"{error_type} | {service} | Root cause: {diagnosis[:200]} | Fix: {fix[:150]}"`
- **context_text** (stored as document): full text returned to the caller

File: `app/services/rag.py:index_incident` + `search_incidents`

### 3. Configurable similarity threshold + score distribution endpoint
- Add `rag_similarity_threshold: float = 0.80` to `app/core/config.py`
- Read it in `app/services/incident_loop.py` instead of hardcoded `0.80`
- Add `GET /debug/rag/scores` — runs test queries, returns histogram of score buckets (0.5–0.6, 0.6–0.7, …) so you can see where the natural gap is

Files: `app/core/config.py`, `app/services/incident_loop.py`, `app/api/routes/debug.py`

### 4. LLM reranking
When corpus ≥ N incidents, retrieve top-10 by cosine, then pass to Haiku to rerank by true relevance.
Adds latency + cost — only justified when false positives start appearing.

- Add `rag_rerank_min_corpus: int = 10` to config
- Implement reranker in `search_incidents` guarded by corpus size check

File: `app/services/rag.py:search_incidents`

---

## Agentic RAG

**When to revisit:** Once the incident corpus has 20+ entries. A tool that searches 3 incidents adds no value.

**What it is:** Instead of pre-fetching a past incident before the agent starts, expose `search_incidents` as a registered tool on `DiagnosisAgent`. The agent decides when to search, what to search for, and whether the results are relevant — as part of its ReAct loop.

**Why it's better than passive RAG**
- Agent can reformulate the query if first results are weak (e.g. try "OOM killed" then "exit code 137")
- Agent reasons about whether a retrieved incident is actually applicable, not just above a score threshold
- Agent can combine RAG results with live tool calls (CloudWatch logs, GitHub history) in the same reasoning chain
- Handles partial relevance: "similar pattern, different service — apply root cause but not the fix"

**How to implement**
1. Add `search_incidents` as a registered tool on `DiagnosisAgent` in `app/agents/diagnosis.py`
2. Remove the `prior_context` injection in `incident_loop.py` — the agent fetches it itself when it decides it needs it
3. The tool signature the agent sees: `search_incidents(query: str) -> list of similar past incidents with score and diagnosis`

**What to remove**
- Layer 3 RAG pre-fetch in `app/services/incident_loop.py` (the `self._rag.search_incidents(query)` block)
- `prior_context` parameter threading through `_run_diagnosis` → `DiagnosisAgent.diagnose`
- The `rag_hit` dedup stat counter (agent-driven retrieval doesn't fit the linear pipeline model)

**Trade-off**
Adds one extra LLM reasoning step per cold-start incident. Worthwhile once the corpus is large enough that retrieval quality matters.

---

## CodeReviewAgent — Codebase-Aware Reviews

Implemented in `feat/codebase-aware-code-review`. Shipped.

---

## FixGenerationAgent — GitHub Code Search + RAG File Resolution

**Context:** See `temp-notes.md` for full analysis. Stack trace parsing is implemented (Strategy 1). Two more layers remain.

### Strategy 2 — GitHub Code Search API
Search file *contents* for the error type string — finds the actual handler file regardless of its name.

```
GET /search/code?q={error_type}+repo:{owner}/{repo}&type=code
```

Add `search_code(owner, repo, query)` to `GitHubService`, call it in `_resolve_target` after stack trace parsing fails. Pass top-3 results with matched snippets to the LLM.

Files: `app/services/github.py`, `app/agents/fix_generation.py:_resolve_target`

### Strategy 3 — RAG Semantic Search for Fix Target
When the codebase is indexed, search semantically using the diagnosis text. Returns the actual code chunk — inject directly into the fix prompt instead of fetching the whole file.

Pre-condition: `CODEBASE_PATH` set + `POST /debug/rag/index` called.

File: `app/agents/fix_generation.py:_resolve_target`

---

## Auto Re-index Codebase on PR Merge

**Problem:** The codebase RAG index goes stale as code changes. New files aren't indexed, refactored files leave ghost chunks, deleted files remain as dead weight.

**Why time-based re-indexing is wrong:** A weekly cron re-indexes even when nothing changed (wasteful) and misses rapid-change periods (stale). The right trigger is a PR merging to main.

**How to implement:**
1. In `app/api/routes/webhooks.py`, handle `pull_request` events with `action: closed` and `merged: true`
2. Fire `rag.index_directory(settings.codebase_path)` in the background via `asyncio.ensure_future`
3. Log chunk count so you can see drift over time

**Why re-indexing is cheap:** `index_directory` uses `upsert` keyed on `sha256(file_path:start_line)`. Unchanged files are a no-op at the ChromaDB level — only actually-modified chunks cost an embedding API call.

**Pre-condition:** The target repo must be checked out locally at `CODEBASE_PATH` and kept up to date (e.g. a `git pull` before indexing, also triggered by the webhook).

File: `app/api/routes/webhooks.py`
