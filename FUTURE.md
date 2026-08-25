# Future Functionalities

Features deferred until there is enough real production data to justify them.
Come back to these after the incident corpus has grown and the eval script surfaces actual problems.

---

## MonitorGenerationAgent

**When to implement (revisit):** After at least one full incident-to-merge cycle has
been observed in production and there is a concrete need to auto-generate CloudWatch
alarms for new code paths.

**What this is:** An agent that fires on every merged PR to the target repo. It reads
the diff, identifies new code paths (functions, routes, error handlers), and generates
CloudWatch alarm configs — one alarm per ~75 lines of changed code. Runs as a dry-run
by default; set `CREATE_MONITORS=true` to provision alarms in AWS. Wired via
`background_tasks.add_task(_run_monitor_generation, ...)` in `app/api/routes/webhooks.py`.

**Why deferred:** The agent generates configs but never provisions them
(`CREATE_MONITORS` was never set in production). It has never been used and adds
complexity to the pipeline diagram without delivering value yet.

**Pre-condition:** At least one full incident cycle resolved in production. `CREATE_MONITORS=true`
set and tested. Alarm naming conventions agreed on to avoid drift.

---

## OpenTelemetry Standardization

**When to implement:** When there is a concrete reason to move off Langfuse — e.g., existing
OTel infrastructure, multi-service distributed traces, or toolchain integration with Jaeger/Zipkin.

**What this is:** Standardize tracing on OpenTelemetry. Define a structured span
hierarchy (session → task → verification step), pipe to an OTel-compatible backend.

**Why deferred:** Langfuse v4 already captures agent/LLM/tool span trees with metadata
and error context. OTel adds infrastructure complexity (collector, backend) with no
concrete benefit at current single-service scale.

**Pre-condition:** Existing OTel infrastructure in the stack, or a multi-service
architecture where distributed traces across services are needed.

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

## Layer 3 End-to-End Verification

**When to implement:** When a staging environment with real (or contract-tested)
external services is available.

**Why unit tests alone aren't enough:** The test suite is fully mocked — no live
API calls. Unit tests cannot catch interface mismatches between components, state
propagation errors across layers, or environment-specific failures. These only
surface when the full system runs.

**What this would be:** A test suite that exercises the full incident pipeline from
`POST /events` through triage → diagnosis → fix → approval, using real or
contract-tested external services (Anthropic API, GitHub, AWS).

**Pre-condition:** Staging environment with real services, or contract tests
(e.g. Pact) that verify integration boundaries without live calls.

**Related idea — an independent evaluator:** A separate agent that reviews completed
work critically, distinct from the agent that generated it — the same model
generating and evaluating tends to be generous to itself. The existing Haiku
self-critique pass in `FixGenerationAgent` already does this for fix generation
specifically; a general-purpose evaluator would extend the pattern further, but
needs real e2e execution evidence to assess against, not just code.

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

Shipped.

---

## FixGenerationAgent — File Resolution Strategies

**Status:** Strategy 1 (stack trace parsing) and Strategy 2 (GitHub Code Search,
`_resolve_target` in `app/agents/fix_generation.py`) are both implemented. Strategy 3
below is the remaining gap.

### Strategy 3 — RAG Semantic Search for Fix Target
When the codebase is indexed, search semantically using the diagnosis text as a final
fallback when stack trace parsing and code search both come up empty. Would return the
actual code chunk — inject directly into the fix prompt instead of fetching the whole file.

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

---

## Adversarial Second-Pass Diagnosis Check

**When to implement:** Once `scripts/measure_diagnosis_grounding.py` shows a real
rejection rate from `submit_diagnosis` (see DECISIONS.md) that justifies the added
LLM-call cost and latency.

**What this is:** The `submit_diagnosis` gate verifies that cited evidence is *real*
(a real file, a real verbatim snippet) — it does not verify the evidence actually
*supports* the conclusion drawn from it. A real file and a real snippet wired to a
conclusion the snippet doesn't actually support would pass every current check. A
second, adversarial pass — "does this evidence actually prove this claim?" — would
close that gap, but it's a softer, more expensive, more failure-prone kind of
judgment than a string match.

**Why deferred:** Not enough production data yet on how often the existing gate
actually rejects something, so there's no way to tell whether this residual gap is
worth the extra cost.

**Pre-condition:** Real rejection-rate data from `scripts/measure_diagnosis_grounding.py`
post-deploy.

---

## Multi-Target Support

**When to implement:** When there's an actual second target running through this
platform simultaneously — not built now, so the design doesn't get shaped by
guesswork about a target that doesn't exist yet.

**What this is:** Right now there's exactly one target (`settings.fix_target_repo`,
one global config value, read once at agent construction time). Real multi-tenant
platforms handle "more than one of these" with a plugin/adapter pattern — Terraform's
providers, Kubernetes' CSI/CNI plugins: one core engine, swappable adapters, a
manifest per target declaring which adapters apply. Concretely here that would mean:
- A `Target` config object (name, repo, log groups, which `app/integrations/`
  adapters apply to it)
- `ErrorEvent`/`IncidentState` gaining a `target` field so routing knows which
  config to load — currently implicit, since there's only one possible target
- `app/integrations/` agents becoming parameterized by that config instead of
  reading global settings directly

**Why deferred:** `app/integrations/` already separates target-specific tooling
(CI/CD, deployment, performance) from the core pipeline structurally — that's the
adapter half of the pattern. What's missing is the per-target config/manifest layer,
which only matters once there's a second target to route between.

**Pre-condition:** A real second target actually running through the platform.
