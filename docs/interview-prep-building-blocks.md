# AI Engineer SD Building Blocks

*Interview reference doc. Five patterns appear across every AI-Eng archetype. Rotate one per SD warm-up: sketch on paper or whiteboard, aloud, under 2 min, before every SD block. The goal is automatic — no retrieval lag in a real interview.*

*Format per block: sketch template (what to draw) → key decisions (what to say when asked "why") → evidence from your system (concrete, not textbook) → common probes + 30-second answers → failure modes to close with.*

---

## 1. RAG Pipeline

> **Two diagrams.** The first is what the current code does. The second lists what was explored per step (from the blog series) and why each was or wasn't shipped. Draw the first in an interview; cite the second when asked "what else did you consider?"

### Diagram 1 — Current code (what's deployed)

```
INDEX PATH
──────────
[Source files]
      ↓
[Chunker]
  JS/TS → function-boundary (regex + brace-depth tracking)
  Python → 50-line fixed window, 10-line overlap
      ↓
[Hash check]  ←── content_hash in doc_chunk_registry
  unchanged → skip (skips 60–80% of calls on incremental re-index)
      ↓
[Chunk registry ops]
  fetch old chunk_vector_ids → delete from ChromaDB → mark superseded → register new
      ↓
[Embed Model]  (text-embedding-3-small)
      ↓
[Vector Store]  (ChromaDB)


QUERY PATH — Code index (DiagnosisAgent + FixGenerationAgent)
──────────────────────────────────────────────────────────────
[Query string]
      ↓
[Embed Model]
      ↓
[Hybrid Search]
  score = 0.7 × cosine_similarity + 0.3 × lexical_token_match
  min_score = 0.45  (filters ~30% of low-relevance results)
  n_results = 4–5
      ↓
[Top-k chunks returned]  ← file_path + start_line + end_line + text
      ↓
[LLM]


QUERY PATH — Incident index (DiagnosisAgent, incident_loop.py)
──────────────────────────────────────────────────────────────
[Error event string]
      ↓
[Embed Model]
      ↓
[Vector search — recall stage]
  min_score = 0.80  (candidate floor)
  n_results = up to 20
      ↓
[Cross-encoder rerank — precision stage]
  model: ms-marco-MiniLM-L-6-v2
  scores each (query, doc) pair → logit score
  returns top 3
      ↓
[Past incidents returned]  ← error_type + description + root_cause + fix_description
      ↓
[LLM]
```

---

### Diagram 2 — Alternatives explored at each step

**Chunking**

| Approach | What it does | Status | Why |
|---|---|---|---|
| **Fixed line-count** | 50 lines, 10-line overlap | Shipped for Python | Simple, good enough for prose-like Python |
| **Function-boundary** | One chunk per JS/TS function via regex + brace depth | Shipped for JS/TS | Score 0.56→0.71, rank 3→1. Fixed split problem and dilution problem |
| **AST-based** (tree-sitter) | Language-aware parse tree extraction | Not shipped | Requires tree-sitter dependency; function-boundary regex covers the JS/TS case |

**Index enrichment (vocabulary gap)**

| Approach | What it does | Status | Why |
|---|---|---|---|
| **No enrichment** | Embed raw code only | Current code | Works for code-vocabulary queries |
| **LLM description appended** | 1-sentence description per chunk, appended before embedding | Explored (Part 4) | NoSuchKey: not-found → rank 7. Rank 7 ≠ top-3. Dilution: description tokens shift embedding away from identifier matches |
| **Separate description chunk** | Description stored as a second chunk | Explored (Part 4) | Better separation. Not shipped: doubles chunk count |
| **Known-incidents KB** | Symptom→root-cause mappings | Shipped (incident index) | More reliable than LLM description for exact-match incident lookup |

**Query-side**

| Approach | What it does | Status | Why |
|---|---|---|---|
| **Embed query directly** | text-embedding-3-small on raw string | Current code | Simple, deterministic, no extra LLM call |
| **HyDE** | LLM generates a hypothetical code function, embed that instead | Explored (Part 11) | Queries 1–3: 0.77→0.84 (better). Vocab-gap query: rank 3→5 (worse). LLM generates expected function, not actual one. Non-deterministic. Not shipped |
| **Query rewrite** | LLM expands query with source-code vocabulary | Not shipped | HyDE is a superset — if the hypothetical fails, rewrite would too |

**Retrieval**

| Approach | What it does | Status | Why |
|---|---|---|---|
| **Pure vector** | cosine similarity only | Legacy (before PR #135) | `classifyFields` scored 0.24 and ranked 2nd despite appearing verbatim — single identifier diluted across all tokens |
| **Hybrid search** | 0.7 × vector + 0.3 × lexical | Current code | Lexical rescues exact identifier matches. min_score=0.45 drops noise |
| **BM25 / pure lexical** | Token frequency only | Not shipped | Fails on semantic / natural-language queries |

**Reranking**

| Approach | What it does | Status | Why |
|---|---|---|---|
| **No reranking** | Return hybrid search top-k directly | Current code for **code search** | Hybrid search with min_score is sufficient; cross-encoder adds latency |
| **Cross-encoder** (`ms-marco-MiniLM-L-6-v2`) | Full (query, doc) pair → logit score | **Shipped for incidents** (`rerank_incidents`, min_score=0.80 candidate floor) | 10.6-point spread (-2.31 to +8.31) vs vector's 0.033 range — 320× wider signal. O(K) cost justified for incident lookup (rare, high stakes). Not justified for every code search call |

---

### Key Decisions

**Chunking strategy.** Fixed line-count has two failure modes: (1) split — function declaration in chunk N, body in chunk N+1; (2) dilution — boilerplate lines dilute semantic signal. Function-boundary chunking eliminates both. Score 0.56 → 0.71 (+0.15), rank 3 → 1. Trade-off: JS/TS only; Python uses 50-line fixed window with 10-line overlap.

**Embedding model choice.** Use the same model at index and query time — mismatches are silent and catastrophic. Code search tops out ~0.70 vs incident search ~0.85 — different domains, different score floors.

**Vector store.** ChromaDB for single-node <1M chunks. pgvector for a single Postgres operational surface. Pinecone/Weaviate for multi-tenant isolation at scale. Critical rule: the vector store is an index, not a source of truth — always confirm live state against Postgres.

**Hybrid search.** Pure vector fails on exact identifier queries — `classifyFields` scored 0.24 and ranked second despite appearing verbatim. `score = 0.7 × vector + 0.3 × lexical`. Anti-pattern: `hybrid_search()` was implemented and documented but both agents were still calling `rag.search()` — pure vector, no quality floor. The Retrieve-Everything fix: two-line change per agent, `min_score=0.45` removes ~30% of noise.

**Cross-encoder reranking.** Wired to incident lookup only. `incident_loop.py` calls `rerank_incidents(min_score=0.80)` — vector search fetches up to 20 candidates above 0.80, cross-encoder re-scores each (query, doc) pair and returns top 3. Vector scores on the same candidate set span 0.033 (noise); cross-encoder spans 10.6 points — 320× wider signal. O(K) cost is justified for incidents (rare, high-stakes); not justified for every code search call.

**Document chunk registry.** ChromaDB upsert never deletes — without a registry, re-indexing accumulates ghost chunks that surface in queries forever. Registry: `(doc_id, chunk_vector_id, content_hash, status)`. Hash check skips 60–80% of embed calls on incremental updates.

**Two separate collections.** Incident index (symptom→root-cause, two-stage: vector min_score=0.80 + cross-encoder) and code index (function-boundary chunks, hybrid search min_score=0.45) are kept separate. Combining them forces a single embedding space to represent both, degrading quality on both.

---

### Evidence from Your System

| Claim | File | Lines |
|---|---|---|
| Function-boundary chunking (JS/TS) | app/services/rag.py | 120–175 |
| Hybrid search formula (α=0.7) | app/services/rag.py | 304–385 |
| Cross-encoder rerank (incidents only) | app/services/rag.py | 579–625 |
| Document chunk registry | app/services/rag.py | 714–786 |
| Live store vs index (status check) | app/services/incident_loop.py | 440–460 |
| Naive→hybrid upgrade in DiagnosisAgent | app/agents/diagnosis.py | 447 |
| Naive→hybrid upgrade in FixGenerationAgent | app/agents/fix_generation.py | 1767 |
| Score 0.56→0.71 on function-boundary | blog: code-rag-function-boundary-chunks.mdx | — |
| Identifier failure / hybrid fix | blog: code-rag-vocabulary-gap.mdx | — |
| Ghost chunk problem / registry fix | blog: code-rag-document-registry.mdx | — |
| Cross-encoder score spread | blog: code-rag-cross-encoder-reranking.mdx | — |
| HyDE: better on 3/4, worse on vocab-gap | blog: code-rag-hyde.mdx | — |

Concrete numbers: 0.56→0.71 score (+0.15), rank 3→1 from function-boundary chunking. Cross-encoder: 10.6-point spread vs 0.033 vector range (320× wider). Hash check skips 60–80% on incremental re-index. min_score=0.45 removes ~30% noise. HyDE: vocab-gap query rank 3→5 (worse).

---

### Interview Answer Structure

**"Walk me through your RAG pipeline."**

> "Index path: function-boundary chunking for JS/TS — one function, one chunk — with a hash-check registry that skips 60–80% of embed calls on incremental re-index. The registry also handles deletion: ChromaDB upsert never deletes, so without it re-indexing accumulates ghost chunks.
>
> Query path: hybrid search — 70% vector, 30% lexical. The lexical boost rescues exact identifier queries that pure vector buries. min_score=0.45 filters ~30% of noise before results reach the prompt.
>
> Two separate indexes: a code index for source retrieval and an incident index for 'have we seen this before?' lookups. The incident path uses two-stage retrieval — vector search at min_score=0.80 for recall, cross-encoder reranking for precision, top 3 returned. The code path uses hybrid search with min_score=0.45, no reranking."

**"What about cross-encoder reranking?"**

> "Wired to incident lookup. `rerank_incidents()` fetches up to 20 candidates via vector search at min_score=0.80, then cross-encoder scores each (query, doc) pair and returns the top 3. Vector scores on that candidate set span 0.033 — noise. Cross-encoder spans 10.6 points (-2.31 to +8.31) — 320× wider signal, much cleaner discrimination. Cost is O(K) inference per incident lookup — justified for a rare, high-stakes call. Not justified for every code search call, which stays on hybrid search only."

**"What about the vocabulary gap — runtime errors not in source code?"**

> "Explored LLM-generated descriptions appended to chunks at index time. Moved the vocabulary-gap query from not-found to rank 7 — progress, but not top-3. Also tried HyDE at query time: helped code-vocabulary queries (0.77→0.84) but made the vocabulary-gap case worse (rank 3→5) — the hypothetical described how the error should be handled, not how it actually is in production. The incident index is the more reliable solution: past resolved incidents indexed by symptom text, retrieved at 0.90 threshold."

---

### Common Probes

**"What if retrieval returns nothing?"** Surface it explicitly. Options: (1) widen min_score or drop it, (2) fall back to BM25 / exact grep, (3) return "couldn't find relevant context" with a prompt constraint against hallucinating. In my system the diagnosis confidence gate (0.70) blocks the pipeline if retrieval is too weak.

**"How do you evaluate RAG quality?"** Recall@3: did the ground-truth chunk appear in the top 3? Golden dataset of 8–20 pairs covering normal, vocabulary-gap, and identifier cases. Baseline first. LLM-as-judge for faithfulness and relevance. Alert when faithfulness drops below 0.7.

**"Shadow index / zero-downtime rebuild?"** Build new index in `codebase_v2` while live queries hit `codebase_v1`. Validate against benchmark queries. Swap atomically via config pointer. Keep `codebase_v1` for 24–48h rollback.

**"Why two separate collections?"** Different retrieval semantics and different score floors (0.45 vs 0.90). Incident index matches on symptom patterns; code index matches on identifiers and structure. Combining them forces a single embedding space to represent both, degrading quality on both.

---

### Failure Modes to Close With

1. **Ghost chunks** — re-index without delete leaves superseded chunks. Chunk registry + explicit delete is the fix.
2. **Stale metadata** — confirm live state from Postgres, not ChromaDB. The vector store is a candidate finder, not a source of truth.
3. **Score calibration mismatch** — code search scores 30–40% lower than prose search; don't reuse the same min_score across domains.
4. **Model mismatch** — different embedding models at index and query time: silent failure, no error thrown.
5. **Retrieve-Everything** — passing all n_results to the LLM regardless of score. min_score=0.45 is the fix; was an active bug before PR #135.
6. **Context overflow** — unlimited top-k eventually exceeds the prompt window; always cap and rank by score.

---

### Key Numbers

| Number | What it is |
|---|---|
| 0.7 / 0.3 | Hybrid search weights: vector / lexical |
| 0.45 | min_score for code search — filters ~30% of naive results |
| 0.90 | min_score for incident search — high bar, match or skip |
| 60–80% | Hash check skip rate on incremental re-index |
| 0.56 → 0.71 | Similarity score gain from function-boundary chunking |
| rank 3 → 1 | Rank improvement from function-boundary chunking |
| 10.6 pts / 0.033 | Cross-encoder score range (-2.31 to +8.31) vs vector range (0.2818–0.3146) — 320× wider signal |
| rank 7 | Where LLM description placed the vocabulary-gap query (not good enough) |
| rank 3 → 5 | HyDE made the vocabulary-gap query worse |

---

---

## 2. Agent Loop

> **Two sketches.** TriageAgent is a fast, cheap classifier — draw it to show model routing. DiagnosisAgent is the full ReAct loop with grounding — draw it to show safety rails and multi-step reasoning.

### Sketch A — TriageAgent (Haiku · single-pass classifier)

```
[ErrorEvent from CloudWatch]
        ↓
[System prompt: classify real/noise/duplicate, assign P0–P3]
[Tool registry: check_duplicate_pr, get_occurrence_count]
        ↓
┌──────[LLM: Thought + Action]──────────────────────┐
│  check_duplicate_pr(error_type, service)           │
│  get_occurrence_count(error_type, hours=24)        │
│               ↓                                   │
│       [Observation]                               │
│               ↓                                   │
│   [Append to messages, loop back] ←───────────────┘
│               ↓ (if Answer:)
└──────→ [Structured JSON: decision + severity]
               ↓
  decision="noise"/"duplicate" → drop
  decision="real", P0–P3 → DiagnosisAgent
```

Annotate: Haiku — 10× cheaper than Sonnet, sufficient for 3-way classification on short error text. `check_duplicate_pr` is a hard gate — the model MUST call it before it can output `decision="duplicate"`.

---

### Sketch B — DiagnosisAgent (Sonnet · full ReAct loop)

```
[IncidentState from TriageAgent]
        ↓
[System prompt: root cause + confidence score]
[Tool registry: search_codebase, get_file_contents,
 search_similar_incidents, verify_symbol_in_repo,
 get_cloudwatch_logs, search_github_code, ...]
        ↓
┌──────[LLM: Thought + Action + Action Input]──────┐
│  search_similar_incidents(symptoms)              │ ← rerank_incidents()
│  search_codebase(query)                          │ ← hybrid_search()
│  get_file_contents(file_path)                    │
│  verify_symbol_in_repo(symbol)                   │ ← GitHub Code Search
│               ↓                                  │
│       [Observation appended]                     │
│               ↓                                  │
│   [loop back, max 10 iterations] ←───────────────┘
│               ↓ (if Answer:)
└──────→ [Structured JSON: root_cause + confidence]
               ↓
  [Server-side grounding guard]
    rejects fabricated camelCase identifiers
    verifies every named function exists in repo
               ↓
  confidence < 0.70 → escalate=True → human review
  confidence ≥ 0.70 → FixGenerationAgent
```

Annotate:
- `verify_symbol_in_repo` is a tool the LLM calls voluntarily AND a server-side hard check — two layers
- Context compression fires at 70% of window — summarizes middle turns, preserves first + last N
- Confidence gate is deterministic (threshold=0.70), not another LLM call

---

### Key Decisions

**ReAct pattern (Reasoning + Acting).** Each iteration: LLM emits a `Thought:` (reasoning), `Action:` (tool name), `Action Input:` (JSON params). The framework executes the tool, appends `Observation:` as a user turn, and the LLM sees its own prior reasoning in the next call. This interleaving lets the model adapt its plan mid-execution based on real observations rather than reasoning in a vacuum. Contrast with chain-of-thought (reasoning only, no tool execution) and plan-then-execute (all planning first, execution second — brittle when the plan hits unexpected state).

**Tool registry design.** Tools are registered as `(name, async callable, description)`. The description is the only documentation the LLM sees — it's the API contract. Names must be exact-match. Descriptions should state: what the tool does, what it returns, and any preconditions. Bad description = silent wrong tool selection.

**Context compression.** At 70% of the context window, compress middle turns: summarize them into a compact representation, preserve the first system prompt and the most recent turns. Without compression, long-running agents hit the context limit and either fail or silently truncate.

**Max iterations.** Cap at 10 iterations. An agent that loops indefinitely is either hallucinating tool calls or stuck in a cycle. On exceeding the cap, return a graceful "unable to find an answer within the allowed number of steps" rather than an error.

**Confidence gate between stages.** In a multi-agent pipeline, each stage should gate on the confidence of the previous stage before proceeding. Diagnosis agent confidence < 0.70 → don't proceed to fix generation. This prevents propagating a weak diagnosis into a bad fix and burning LLM budget downstream.

**Circuit breaker for tool calls.** Wraps every external tool call. Three states: CLOSED (normal), OPEN (failing — reject calls immediately), HALF_OPEN (testing recovery). Parameters: `failure_threshold=5`, `timeout=60s`, `success_threshold=2`. Prevents retry storms against a degraded service.

---

### Evidence from Your System

| Claim | File | Lines |
|---|---|---|
| ReAct loop (Thought/Action/Observation) | app/agents/base.py | 273–337 |
| Tool registration | app/agents/base.py | 251–260 |
| Context compression at 70% | app/agents/base.py | 282–293 |
| MAX_ITERATIONS enforcement | app/agents/base.py | 330–335 |
| Confidence gate (0.70 threshold) | app/agents/diagnosis.py | 41 |
| Symbol grounding + known incidents KB | app/agents/diagnosis.py | 47–102 |
| Circuit breaker state machine | app/services/circuit_breaker.py | 42–176 |
| Approval gate (LOW/MEDIUM/HIGH/CRITICAL) | app/services/approvals.py | 30–166 |

Model routing: Haiku for triage (10x cheaper than Sonnet, sufficient for 3-way classification on short error text), Sonnet for diagnosis + fix (needs multi-step reasoning over file content), GPT-4.1 for code review (independent model perspective).

---

### Common Probes

**"What prevents the agent from running forever?"** Hard cap at MAX_ITERATIONS. Circuit breaker on tool calls (5 failures → OPEN → reject calls). Per-step observation is appended as a user message — if tools consistently return errors, the model will reason about the failure and emit an Answer.

**"How does memory work across iterations?"** Short-term memory = the messages list. Long-term memory = a persistent store (SQLite) holding incident state, past diagnoses, past PR outcomes. The agent queries the long-term store via tools, not by having everything in context. Context compression bridges the two.

**"What's a guardrail vs a constraint in your system?"** Constraints live in the system prompt ("do not generate fixes that add null guards at the crash site"). Guardrails live in the execution layer and can block an action regardless of what the LLM said ("blast radius > 5 files → hard stop"). I put guarantees where correctness is non-negotiable (blast-radius, DoD gate) and hints where judgment is acceptable (self-critique, confidence nudges).

**"How do you handle a tool that returns bad data?"** Observation is appended verbatim — the model sees "Error: file not found" as an observation and adapts.

---

### Failure Modes to Close With

1. Symptom-fix anti-pattern — agent adds a null guard at the crash site instead of diagnosing the root cause. Requires a deterministic detector in the DoD gate, not just a prompt instruction.
2. Blast radius unbounded — agent modifies auth files or 20 files in a single PR. Hard block on file count (>5) and line count (>500) before the PR is opened.
3. Stale symbol grounding — error type appears in a comment in the wrong file. Fix: resolve file only from the stack trace, not from semantic search over error type strings.
4. FIX_FAILED loop — incident status is FIX_FAILED but the dedup gate doesn't recognize it as terminal and keeps re-running.
5. Context compression losing critical state — preserve the first user turn and the last N turns; summarize only the middle.

---

---

## 3. Evaluation Harness

### Sketch (3 layers)

```
Layer 1 — UNIT: Component-level, offline
  [Retrieval: Recall@K] [Triage: Accuracy on golden set] [Fix: Pass rate on sandbox]

Layer 2 — INTEGRATION: End-to-end, offline golden dataset
  [Input alarm] → [Full pipeline] → [Compare output vs expected]
  Metrics: MTTR, false-positive rate, PR merge rate

Layer 3 — PRODUCTION: Online monitoring
  [Langfuse traces] → [LLM-as-judge (faithfulness, relevance)]
  → [Alerts on score drop]
  → [Human feedback loop (HITL labels on low-confidence outputs)]
```

Annotate while drawing:
- "Define success before architecture — weak candidates skip this"
- "LLM-as-judge caveats: self-preference bias, anchoring"
- "Shadow evaluation before traffic migration"

---

### Key Decisions

**Define success metrics before writing any architecture.** The most common weak candidate move is drawing boxes and then asking "how would we evaluate this?" at the end. The eval should define what good looks like for each component, which drives the architecture decisions.

**Golden dataset construction.** Must cover: normal cases, edge cases (vocabulary gap, ambiguous queries, near-duplicate incidents), and adversarial cases (prompt injection in the alarm body, missing stack traces). 20 examples is enough to catch regressions; 100 is enough to estimate accuracy with reasonable confidence intervals.

**Recall@K for retrieval.** For each (query, ground-truth chunk) pair: did the ground-truth chunk appear in the top K results? K=3 is the right threshold for RAG context assembly. Baseline before any changes; treat a regression as a blocking issue.

**LLM-as-judge for generation quality.** Two dimensions: faithfulness (does the generated answer stay within the retrieved context?) and relevance (does the answer actually address the question?). Caveats: (1) self-preference bias — use a different model family as judge when possible; (2) anchoring — evaluate against criteria before seeing the output; (3) calibration — LLM scores are ordinal, not interval.

**Fix-quality evaluation.** Two-phase sandbox: (1) baseline run on unpatched code establishes which tests were already failing; (2) apply fix, run again, compare. Only new failures count against the fix.

**HITL labeling.** Low-confidence outputs get routed to a human review queue. Labels feed back into the golden dataset and into prompt refinement. Without this loop, the harness drifts from production reality over time.

**Shadow evaluation.** Before migrating traffic to a new model version or chunking strategy: run the new system in shadow mode alongside the live system. Compare outputs on the same inputs. Only migrate when the shadow system matches or beats the live system on the golden dataset.

---

### Evidence from Your System

| Claim | File / Location |
|---|---|
| Recall@3 harness, 8/8 baseline | blog: code-rag-search-quality.mdx |
| OTel spans on RAG requests | blog: code-rag-observability.mdx |
| LLM-as-judge (faithfulness, relevance) | blog: code-rag-observability.mdx |
| Sandbox two-phase baseline | app/services/sandbox.py:56–71 |
| Langfuse tracing integration | app/services/tracing.py:39–66 |
| 4 merged PRs, distinct failure classes | Honest framing locked in prep plan |

Key numbers: 8/8 recall@3 on retrieval (small-N, honest limitation — detects regressions, doesn't estimate population accuracy).

---

### Common Probes

**"How do you know when the LLM is hallucinating vs the retrieval is bad?"** Attribution. Every answer must trace back to a specific chunk. Without chunk-level attribution in traces, every bad answer looks identical from outside.

**"What's your false positive rate?"** Honest answer: 4 merged PRs across 4 distinct failure classes (null guard, S3 existence check, dedup parallel write, LLM wrapper incomplete return), each with a passing sandbox and a human-reviewed PR. That's a small-N demonstration, not a population estimate.

**"How do you handle distribution shift?"** Continuous logging of every production run with outcome labels; periodic re-labeling pass for recent low-confidence outputs; alert on faithfulness drop < 0.7.

---

### Failure Modes to Close With

1. Eval as afterthought — defining metrics after architecture means measuring what's easy, not what matters.
2. Golden dataset leakage — if examples were used to tune the system, the eval measures memorization.
3. LLM judge miscalibration — a generous judge masks quality degradation.
4. Sandbox mock drift — 9 pre-existing test failures in agent-platform from mock drift.
5. Metric gaming — optimizing Recall@3 can reduce precision; track both.

---

---

## 4. Guardrail Layer

### Sketch (three tiers: pre-fix, in-loop, post-fix)

```
[Triaged incident]
        ↓
PRE-FIX GUARDRAILS (before fix generation starts)
  • Confidence gate — diagnosis confidence < 0.70 → escalate=True → human review, no fix
  • Symbol grounding guard — server-side rejects fabricated function names
      (LLM calls verify_symbol_in_repo voluntarily AND server re-checks every named
       function in parsed output — two independent layers)
        ↓
[FixGenerationAgent — ReAct loop, up to 14 iterations]
        ↓
IN-LOOP GUARDRAILS (inside fix generation)
  • Blast-radius guard — >5 files or >500 lines changed → hard block, regenerate
  • Self-critique (Haiku) — structured checklist: addresses root cause? no null guards?
      LIKELY WRONG → switch to alternate reasoning frame, retry up to 3×
  • Sandbox gate — fix runs Jest suite in Docker; fails → regenerate up to 3×
        ↓
POST-FIX GUARDRAILS (before any merge)
  • DoD gate — 5 hard checks: no null guards, blast radius addressed, test evidence, etc.
  • Cross-provider review — GPT-4.1 reviews Sonnet's output; different model family
      enforced at startup (gateway raises if both providers resolve to the same family)
  • Approval gate — ALL PRs require human sign-off
      HIGH/CRITICAL → immediate escalation, cannot auto-proceed
      rejections logged as RLHF preference pairs
```

Annotate while drawing:
- "Hard blocks are deterministic — blast-radius, DoD gate, approval gate can't be prompted around"
- "Soft hints shape output — self-critique and confidence nudges are LLM-shaped"
- "Two independent grounding layers: LLM-voluntary tool call + server-side hard check"
- "Cross-provider bias: the reviewer is structurally incapable of being lenient with its own output"

---

### Key Decisions

**Hard blocks vs soft hints.** Hard blocks live in the execution layer and fire regardless of what the LLM said. Soft hints live in the prompt and shape output where judgment is acceptable. The failure mode of using soft hints for safety-critical paths: the LLM can reason its way around a hint. Hard blocks are deterministic. Rule: blast radius, DoD gate, approval gate = hard blocks. Self-critique, confidence nudges = soft hints.

**Two independent grounding layers.** DiagnosisAgent calls `verify_symbol_in_repo` as a tool (LLM decides when). The server also runs `_enforce_grounding()` on the parsed output regardless of what the LLM did — it re-checks every named function and rejects fabricated camelCase identifiers. The LLM-voluntary call catches hallucinations during reasoning. The server-side check catches anything that slipped through.

**Confidence gate is deterministic, not LLM-shaped.** `CONFIDENCE_THRESHOLD = 0.70` in `diagnosis.py:41`. If the diagnosis confidence is below 0.70, `escalate=True` and fix generation never starts. This is a hard input guard — a weak diagnosis cannot propagate downstream and burn LLM budget on a bad fix.

**Cross-provider code review.** Fix generation uses Claude Sonnet. Code review uses GPT-4.1. Enforced at startup — the gateway raises on boot if both resolve to the same provider. Different model families have different blind spots; the reviewer is structurally incapable of being lenient with its own output. This is the same principle as separation of duties.

**Sandbox before PR.** Every fix runs in Docker against the real Jest suite. If tests fail, regenerate up to 3× before opening any GitHub noise. The sandbox runs a baseline (pre-patch) first — only fails the run if the fix introduces *new* failures beyond the baseline.

**Approval gate before every merge.** Not just HIGH/CRITICAL — all PRs require human approval. HIGH/CRITICAL also triggers an immediate escalation path. Rejections are logged as RLHF preference pairs for future fine-tuning.

---

### Evidence from Your System

| Claim | File | Lines |
|---|---|---|
| Confidence gate (CONFIDENCE_THRESHOLD = 0.70) | app/agents/diagnosis.py | 41 |
| Symbol grounding guard (server-side) | app/agents/diagnosis.py | 665–740 |
| Blast-radius guard | app/services/blast_radius.py | — |
| Self-critique + LIKELY WRONG retry | app/agents/fix_generation.py | 374–414 |
| Sandbox two-phase baseline | app/services/sandbox.py | 56–71 |
| DoD gate (symptom-fix detector + 4 other checks) | app/agents/fix_generation.py | 1545 |
| Cross-provider enforcement at startup | app/services/llm_gateway.py | — |
| Approval gate (HIGH/CRITICAL escalation) | app/services/approvals.py | 140–166 |

Core principle: **"Put guarantees where correctness is non-negotiable (blast-radius, DoD gate, approval gate) and hints where judgment is acceptable (self-critique, confidence nudges)."** This is the answer to any guardrail design question in an interview.

---

### Common Probes

**"How do you prevent the agent from making unsafe changes?"** Three independent stops: blast-radius guard catches scope creep before a PR opens; DoD gate catches symptom fixes and missing tests; approval gate catches everything else. Any one of the three can block the change. They're not redundant — each catches a different failure class.

**"What's the difference between a guardrail and a prompt instruction?"** A prompt instruction is a hint — the LLM can reason around it if the context is strong enough. A guardrail is a deterministic check in the execution layer that fires regardless of what the LLM output. In my system: "do not add null guards" is in the prompt (hint) AND the DoD gate explicitly checks for null guards (hard block). The prompt reduces frequency; the gate is the guarantee.

**"How do you prevent hallucinated function names from reaching the fix?"** Two layers: the LLM calls `verify_symbol_in_repo` during reasoning (voluntary grounding), and `_enforce_grounding()` runs server-side on the parsed output (mandatory). If a function doesn't exist in the repo, the diagnosis is rejected and confidence is forced to ≤0.65.

**"Why cross-provider review?"** Same-model judge problem: if Sonnet reviews its own fix, it's biased toward approving its own reasoning patterns. GPT-4.1 has different blind spots, different training, different stylistic priors. The cross-provider constraint is enforced at startup — the gateway raises on boot if both are the same family, so it can't silently degrade.

---

### Failure Modes to Close With

1. **Symptom-fix anti-pattern** — agent adds a null guard at the crash site instead of finding root cause. Requires a deterministic DoD check, not just a prompt instruction — the LLM can justify a null guard if the context is ambiguous.
2. **Blast radius unbounded** — agent modifies auth files or 20 files in a single PR. Hard block on file count (>5) and line count (>500) before any PR is opened.
3. **Confidence gate bypassed** — weak diagnosis (confidence 0.4) proceeds to fix generation, burns LLM budget on a bad fix. The 0.70 threshold is the gate; anything below escalates to human.
4. **Grounding guard incomplete** — LLM names a function in a comment in the wrong file; symbol appears in the repo so `verify_symbol_in_repo` passes, but it's the wrong function. Fix: resolve file only from the stack trace, not from semantic search over error type strings.
5. **Sandbox mock drift** — test suite passes in sandbox using mocks that diverge from production behavior. Baseline-first approach catches new failures; doesn't catch cases where mocks were already wrong before the fix.

---

---

## 5. Caching Strategy

### The 5× token asymmetry (frame this before drawing)

Every LLM pricing table has two numbers. On Sonnet 4.6: $3.00 and $15.00 per million tokens. Output is 5× more expensive because it's generated sequentially — each token requires a full pass through the model and a KV cache update. Input is processed in parallel. This asymmetry shapes every caching decision for agent systems.

In agent loops, output compounds: each iteration's output becomes the next iteration's input. A 400-token observation at iteration 2 gets re-billed on iterations 3–10 — nine more times at input rate. The caching strategy for agent loops has to address two separate problems: the stable prefix billed redundantly on every call, and the growing history carrying data that has already served its purpose.

### Sketch (3 cache tiers + state pruning)

```
[User Request]
      ↓
[Tier 1: Exact cache] — hash(prompt) → stored response
  Hit: return immediately, 0 LLM cost
      ↓ miss
[Tier 2: Semantic cache] — embed(prompt) → vector lookup, cosine sim > threshold
  Hit: return semantically equivalent past response
      ↓ miss
[Tier 3: Prompt cache (provider-side)] — reuse KV activations for shared prefix
  Saves input token cost on repeated system prompt / document context
  cache_control: ephemeral | 77% hit rate on 6-iter diagnosis loop | 13% cost reduction
      ↓ miss
[LLM inference]
      ↓
[Store result in Tier 1 + Tier 2]

[State Pruning] — orthogonal, not a cache tier
  Per-N-iterations: stub stale tool results above threshold
  Targets the growing history, not the stable prefix
  ~18,000 tokens removed per 10-iteration fix run
```

Annotate while drawing:
- "Tier 1 is free; Tier 2 costs one embed call; Tier 3 is automatic if you structure prompts right"
- "Semantic threshold: high (0.97+) for factual Q&A, lower (0.85) for generative tasks"
- "State pruning is orthogonal — caching targets stable prefix, pruning targets stale history"
- "Cache key must include: model version, system prompt hash, context version"

---

### Key Decisions

**Exact cache.** Hash the full input (model + system prompt + user message). Hit rate is low for generative applications but high for structured use cases (classification, extraction, RAG queries over fixed documents). Use Redis or an in-memory LRU with a TTL.

**Semantic cache.** Embed the user query, search against a vector index of past queries, return the response if cosine similarity > threshold. Threshold calibration: for factual Q&A (RAG), use 0.97+ — similar questions might need different answers. For classification tasks, 0.85 is safe because the output space is small. Risk: returns a cached answer that was correct for a similar-but-different question. Mitigate: include the retrieved context in the cache key hash, add a TTL.

**Provider-side prompt cache (Anthropic).** Long, repeated prefixes — system prompts, harness docs, document context — can have their KV activations cached on the provider side. Anthropic charges 10% of input cost to write to cache (125% on first call), 10% on subsequent cache reads. The break-even is ~10 reads.

Implementation: `cache_control: {"type": "ephemeral"}` on the stable content block. The cache key is the exact token sequence — one token difference invalidates it. Structure your prompts to maximize cache reuse: put the stable prefix first (system instructions, harness docs, retrieved documents), put the variable part last (the user's question).

Real numbers from the platform: harness docs prefix = 2,471 tokens. 6-iteration diagnosis loop: 77% cache hit rate, 13% cost reduction ($0.0198 saved per run). At 10–14 iterations (FixGen), expected saving is 15–18% on that step. 5-minute TTL — within-incident savings are reliable; cross-incident savings depend on incident arrival cadence.

**State pruning — complementary to prompt caching.** Prompt caching targets the stable prefix. State pruning targets the growing conversation history — specifically, oversized tool results that have already served their purpose.

In `FixGenerationAgent`, `read_file` tool results are ~1,500–2,000 tokens each. By iteration 5, the agent has read several files; every subsequent iteration re-sends all of that file content in the conversation history. After the edit is applied, the file content is dead weight.

```python
def _prune_tool_results(self, messages, keep_last=2, threshold=500):
    tool_result_indices = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool"
        and len(str(m.get("content", ""))) > threshold
    ]
    to_stub = tool_result_indices[:-keep_last] if len(tool_result_indices) > keep_last else []
    pruned = list(messages)
    for i in to_stub:
        pruned[i] = {**pruned[i], "content": "[tool result pruned — see most recent call for current state]"}
    return pruned
```

Called every 3 iterations in the FixGen loop. `threshold=500` characters catches `read_file` results while preserving short confirmations (`{"status": "ok"}` = ~50 chars, kept). `keep_last=2` retains the two most recent oversized results — the agent needs recent file state.

Real numbers: ~18,000 tokens removed on a 10-iteration run = $0.054 saved per run = 29% reduction on the FixGen step. No overlap with prompt caching: caching targets the stable harness prefix, pruning targets the growing tool-result history.

**Model routing as a cost strategy.** Use the cheapest model that can do the job. Haiku for triage: 10× cheaper than Sonnet, sufficient for 3-way classification. Sonnet for diagnosis + fix. GPT-4.1 for code review. This is a form of caching: routing away from expensive models when the task doesn't require them.

**Cache invalidation.** Triggers: (1) model version change, (2) system prompt change, (3) underlying data change. Strategy: include model version and system prompt hash in the cache key. For RAG responses: include a content hash of the retrieved chunks. Expire entries after a TTL proportional to how often the underlying data changes.

**When not to cache.** Don't cache fix generation — each incident needs a fresh analysis of the current codebase state. Don't cache safety-critical decisions where staleness is dangerous. Don't cache personalized responses that should vary by user.

---

### Evidence from Your System

| Claim | File | Lines |
|---|---|---|
| Prompt cache implementation (`cache_control`) | app/agents/base.py | `_with_harness()` |
| Cache token billing at 10% | app/services/gateway.py | LLMGateway.complete() |
| Cache metadata on LLMResponse | app/models/* | cache_read_input_tokens |
| State pruning implementation | app/agents/fix_generation.py | `_prune_tool_results()` |
| State pruning call site (every 3 iters) | app/agents/fix_generation.py | FixGen loop |
| Model routing config | config/llm_routing.json | all |
| Context compression (70% threshold) | app/agents/base.py | 282–293 |

Concrete numbers: 77% cache hit rate, 13% cost reduction on diagnosis (measured, 6-iteration loop). 18,000 tokens removed per 10-iteration FixGen run, $0.054 saved, 29% reduction on that step. Combined: full-pipeline cost from $0.1861 toward $0.14–0.15 with both optimizations. Haiku: $0.00025/1K input; Sonnet: $0.003/1K input — 12× difference.

---

### Common Probes

**"What's your cache hit rate and how do you measure it?"** Log hits, misses, and tier attribution. For semantic cache, log the similarity score of each hit. Track by request type — RAG queries over the same document set will have higher hit rates than open-ended generation.

**"How do you handle cache poisoning?"** Exact cache: scope cache keys to user/session for personalized responses; validate cached responses before returning (schema check, content policy check). Semantic cache: include a validity timestamp, re-validate high-stakes cached responses.

**"Where does prompt caching help most?"** Long, stable prefixes. A RAG system that always prepends the same 10K-token document context before a query benefits enormously — the document's KV activations are computed once and reused across thousands of queries. Structure matters: put the stable part first, the variable part last.

**"How do prompt caching and state pruning interact?"** They don't — they target orthogonal parts of the token cost. Prompt caching targets the stable prefix (same every iteration). State pruning targets the growing conversation history (different every iteration). You can implement both without conflict; they compound.

---

### Failure Modes to Close With

1. Stale semantic cache — similar query returns a cached answer that was correct last week but the underlying data has changed. TTL + content-hash-in-cache-key mitigates.
2. Cache key collision — two semantically different prompts produce the same hash. For safety-critical paths, prefer exact cache over semantic cache.
3. Prompt cache invalidation on minor changes — one token difference invalidates the entire cache entry. Review prompt templates for unnecessary variability in the stable prefix.
4. Model version staleness — cached response from an older model version may have different safety properties. Include model version in every cache key.
5. Ignoring serving economics — a 50% semantic cache hit rate on 1M calls/day at $0.003/call saves $1,500/day. Present caching decisions with cost math.
6. Pruning too aggressively — if the agent needs to reference earlier file contents that have been stubbed, it will re-read the file and undo the savings. `keep_last=2` is calibrated for 10-iteration FixGen loops; adjust for longer runs.

---

---

## 6. Agent Memory (Four Tiers)

### Sketch (4-tier stack with latency and persistence)

```
Tier / Layer       Type                Technology                     Latency     Persistence
──────────────────────────────────────────────────────────────────────────────────────────────
L1  Working        Context window      KV cache / GPU HBM             <50ms       Ephemeral
L2  Episodic       Past experiences    Vector DB (append-only log)    100–300ms   Persistent
L3  Semantic       Distilled facts     Knowledge graph / SQL          200–800ms   Persistent
L4  Procedural     Skills / workflows  Skills registry / files        50–500ms    Persistent
```

Annotate while drawing:
- "L1: managed by prompt caching + state pruning + context checkpointer"
- "L2: two indexes — incident history and code chunks; append-only, never authoritative for blocking decisions"
- "L3: knowledge graph — not implemented; needs tree-sitter; deferred"
- "L4: harness docs are implicit procedural memory — operating procedures for each agent type"
- "The tier most candidates miss: L4 Procedural"

---

### Key Decisions

**L1 — Working Memory: three management techniques.**

The context window is finite and expensive. Three layers of management:

1. *Prompt caching* targets the stable prefix — harness docs (~2,471 tokens) marked `cache_control: ephemeral`. Billed at 10% on cache reads. 77% hit rate, 13% cost reduction on a 6-iteration diagnosis run.

2. *State pruning* targets the growing conversation history — `_prune_tool_results()` stubs stale `read_file` results (1,500–2,000 tokens each) after they've been processed. Called every 3 iterations, `keep_last=2`. ~18,000 tokens removed per 10-iteration FixGen run.

3. *Context checkpointer* is the last-resort backstop — fires at 70% of the context limit, compresses older turns via summarization. Preserves the first user turn and the most recent N turns; summarizes the middle.

These three target different parts of L1 cost with no overlap: caching saves on the static prefix, pruning removes stale tool results, compression handles pathological long runs.

**L2 — Episodic Memory: append-only, not authoritative.** Two indexes in the platform: the incident index (past resolved incidents, embedded and stored in pgvector) and the code index (function-boundary chunks with hybrid search). Both are queried at the start of a pipeline run; top-k results are injected into L1. Critical rule: L2 is always read-only for blocking decisions. Live Postgres confirms current state; the vector index finds candidates. This prevents stale-read bugs where a ChromaDB metadata field says "in_progress" for an incident that resolved 20 minutes ago.

**L3 — Semantic Memory: the knowledge graph tier.** Not implemented. Would store stable facts — "function X always throws Y under condition Z", "service A depends on service B" — as a queryable graph. Requires: (1) tree-sitter for AST extraction, (2) call graph construction, (3) graph query layer on top of Postgres. When L3 becomes worth it: multiple target codebases with cross-service dependencies, recurring error patterns that require reasoning across call graphs, structural questions ("which services depend on this database table?"). At current scale — one codebase, O(10) incidents/day — L2 is sufficient.

**L4 — Procedural Memory: how to do things.** Stores skills, operating procedures, tool-use sequences — not what happened (L2) or what is true (L3), but the correct process for completing a type of task. In the Anthropic stack this is SKILL.md files. In the agent-platform, harness docs (AGENTS.md, CONSTRAINTS.md) are the closest equivalent: they encode operating procedures for each agent type and are loaded into L1 on every call. Not a formal skills registry, but they serve the same function. A formal L4 would allow self-update (Reflexion-style: after a successful/failed run, distill the lesson into a new procedure) and task-scoped loading (only load skills matching the current task signature).

**Tier selection rule.** The question is not "which tier fits" but "who pays the cost of being wrong":
- A missed retrieval in L2 fails one turn.
- A bad fact in L3 fails every turn until corrected.
- A poisoned skill in L4 propagates to every future invocation.

Put volatile, session-scoped data in L1 only. Put past observations in L2. Graduate a fact to L3 only after N=3–5 independent observations of the same pattern. Graduate an episodic pattern to L4 only if it's a reusable procedure, not a one-off.

**Common tier placement mistakes:**
- Session preferences in L3 — "user wants terse responses in this session" belongs in L1 only; don't pollute L3 with transient state.
- Fast-moving facts in any tier — today's stock price or current weather should never enter memory; call the tool.
- Deployment steps in L2 — "steps to deploy the service" is a procedure (L4), not an event (L2).

---

### Evidence from Your System

| Claim | File | Lines |
|---|---|---|
| L1 prompt caching | app/agents/base.py | `_with_harness()` |
| L1 state pruning | app/agents/fix_generation.py | `_prune_tool_results()` |
| L1 context checkpointer | app/agents/base.py | 282–293 |
| L2 incident index (RAGService) | app/services/rag.py | `_incident_collection` |
| L2 code index (hybrid search, α=0.7) | app/services/rag.py | 304–385 |
| L2 live-store confirmation rule | app/services/incident_loop.py | 440–460 |
| L4 harness docs (implicit procedural) | app/agents/base.py | `_load_harness_docs()` |
| L3 deferred | — | tree-sitter on roadmap |

---

### Common Probes

**"How does your agent system manage memory?"** Four tiers. L1 is the context window — managed with prompt caching on the stable harness docs prefix (77% hit rate, 13% reduction measured), state pruning to remove stale tool results from the growing history, and a context checkpointer as the backstop at 70% of the limit. L2 is episodic — pgvector with two indexes: past incidents and the codebase using function-boundary chunks with hybrid search. Both are queried at run start; the vector store is read-only for blocking decisions, live Postgres confirms truth. L3 is a knowledge graph — not implemented; tree-sitter is the prerequisite. L4 is procedural — currently implicit in the harness docs, a formal skills registry is the natural next step.

**"Which memory tier would you use for X?"** Data goes to L3, procedures to L4, observations to L2. Never store fast-moving facts that have a live source. If a fact is wrong in L3 it fails every turn; if wrong in L4 it propagates to every future invocation — that asymmetry drives placement.

**"When does episodic memory become a liability?"** Three named patterns: index overload (1,000 low-quality observations bury 10 high-quality ones), the Day-30 drift problem (quality degrades as the store fills with noise), and stale-context bleeding (past trajectories that succeeded under one configuration become actively wrong under a new one). The mitigation is a pruning policy from day one — episodic memory without one is technical debt that compounds linearly with usage.

**"What's the difference between L2 Episodic and L3 Semantic?"** L2 is raw observation — "on 2026-05-14, incident #47 was caused by a null pointer in auth.js." L3 is distilled fact — "auth.js throws on null token under concurrent load." L2 is append-only; L3 is updateable. You graduate from L2 to L3 after N=3–5 observations of the same pattern with confidence-weighted corroboration.

---

### Failure Modes to Close With

1. Memory poisoning via prompt injection — untrusted input written to L3/L4 and replayed as authoritative. MINJA (NeurIPS 2025): 70% success rate via query-only injection, no elevated privileges. Mitigation: provenance tags on every write, write-time guardrail that refuses instruction-shaped writes.
2. Stale facts — bitemporal storage (every fact has `valid_from`, `valid_to`, `invalid_at`) and TTL on session-scoped preferences.
3. Conflicting facts — temporal update ("I moved to Berlin") → supersede; correction ("I never said that") → retract with audit trail; outright contradiction → ask the user; never silently overwrite.
4. Day-30 drift — quality degrades ~30 days into production without a pruning policy. Canary fact tests in CI catch this early.
5. Hallucinated memory writes — agent infers a fact, stores it as ground truth, cites it later as authority. Schema-enforced `confirmed_facts` vs `inferred_facts` fields prevent auto-promotion.
6. Cross-tenant leakage — ~95% organic leakage rate in unisolated multi-tenant RAG on benign queries. Physical separation (per-tenant collections), not metadata-filtered shared index.

---

---

## Quick Rotation Card

| Pattern | First box → last box | One-sentence "why" |
|---|---|---|
| RAG | Documents → chunk → embed → hybrid search (α=0.7, min_score=0.45) → top-k → LLM | Vector alone fails on identifiers; hybrid rescues; min_score filters noise; cross-encoder built but not in production path |
| Agent loop | Trigger → tool registry → Thought/Action → tool → Observation → loop → Answer → human gate | ReAct interleaves reasoning with real observations; hard gates at risk decision points |
| Eval harness | Define success → golden dataset → unit/integration/prod layers → LLM-as-judge → HITL | Define metrics before architecture; eval is what converts a demo into a system |
| Guardrails | Input (PII redact + injection + policy) → LLM → output (grounding + schema + policy) → restore | Hard blocks for non-negotiable correctness; soft hints for judgment; never swap them |
| Caching | Exact hash → semantic embed → prompt cache (stable prefix, 77% hit rate) → state pruning (stale history) → LLM | Output tokens 5× cost; agent loops compound; two orthogonal fixes for two orthogonal problems |
| Memory | L1 context (cache+prune+compress) → L2 episodic (pgvector) → L3 semantic (graph, deferred) → L4 procedural (harness) | Each tier has different latency, persistence, and blast radius when wrong |

---

## Drill Protocol

1. Pick one row from the quick rotation card.
2. Set a 2-minute timer.
3. Sketch the boxes and flow on paper, annotate the key decisions aloud.
4. After 2 minutes: what did you skip? Add it to the card.
5. Rotate to the next pattern tomorrow.

The goal is zero retrieval lag — the sketch should flow before you've consciously decided what to draw next.

---

## Key numbers to know cold

| Number | What it is |
|---|---|
| α=0.7 | Hybrid search weight: 70% vector + 30% lexical |
| min_score=0.45 | Quality floor for code search — filters ~30% of naive results |
| 0.56→0.71 | Similarity score gain from function-boundary chunking |
| 10.6 pts / 0.033 | Cross-encoder score range (-2.31 to +8.31) vs vector range (0.2818–0.3146) — 320× wider signal |
| 77% | Prompt cache hit rate on harness docs prefix (6-iter diagnosis loop) |
| 13% | Cost reduction from prompt caching on diagnosis step |
| 18,000 tokens | Removed per 10-iteration FixGen run via state pruning |
| $0.054 | Saved per run via state pruning (29% reduction on FixGen step) |
| $0.1861 | Average per-incident cost; target $0.14–0.15 with both optimizations |
| 5× | Output token cost multiplier over input (Sonnet: $3 vs $15/M) |
| 70% | Context checkpointer trigger (% of context limit) |
| MAX_ITERATIONS=10 | Hard termination guard in BaseAgent |
| 5 failures / 60s | Circuit breaker: OPEN threshold / timeout before HALF_OPEN |
| P0=2, P1=4, P2/P3=6 | Bulkhead semaphore limits by priority lane |
| 4 tiers | L1 Working (<50ms) → L2 Episodic (100–300ms) → L3 Semantic (200–800ms) → L4 Procedural (50–500ms) |
| N=3–5 | Observation threshold to graduate L2 pattern to L3/L4 |
| 70% | MINJA attack success rate (query-only memory poisoning, no elevated privileges) |
| ~95% | Organic cross-tenant leakage rate in unisolated multi-tenant RAG |
| Day-30 | When memory drift starts degrading quality without a pruning policy |
