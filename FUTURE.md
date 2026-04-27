# Future Functionalities

Features deferred until there is enough real production data to justify them.
Come back to these after the incident corpus has grown and the eval script surfaces actual problems.

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
