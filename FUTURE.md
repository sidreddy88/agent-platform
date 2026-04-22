# Future Functionalities

Features deferred until there is enough real production data to justify them.
Come back to these after the incident corpus has grown and the eval script surfaces actual problems.

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
