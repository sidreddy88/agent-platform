# SD Building Block: RAG Pipeline

> **Two diagrams.** The first is what the current code does. The second lists what was explored per step (from the blog series) and why each was or wasn't shipped. Draw the first in an interview; cite the second when asked "what else did you consider?"

---

## Diagram 1 — Current code (what's deployed)

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

## Diagram 2 — Alternatives explored at each step

### Chunking

| Approach | What it does | Status | Why |
|---|---|---|---|
| **Fixed line-count** | 50 lines, 10-line overlap | Shipped for Python | Simple, good enough for prose-like Python |
| **Function-boundary** | One chunk per JS/TS function via regex + brace depth | Shipped for JS/TS | Score 0.56→0.71, rank 3→1. Fixed split problem and dilution problem |
| **AST-based** (tree-sitter) | Language-aware parse tree extraction | Not shipped | Requires tree-sitter dependency; function-boundary regex covers the JS/TS case |
| **Sentence-window** | Small retrieval chunk, large generation chunk | Not shipped | Overlap approximates the benefit cheaply |

---

### Index enrichment (vocabulary gap)

| Approach | What it does | Status | Why |
|---|---|---|---|
| **No enrichment (baseline)** | Embed raw code only | Current code | Works for code-vocabulary queries |
| **LLM description appended** | Generate 1-sentence description per chunk, append to text before embedding | Explored (Part 4) | NoSuchKey went from not-found to rank 7. But rank 7 ≠ top-3. Score dilution: description tokens shift embedding away from identifier matches |
| **Separate description chunk** | Store description as a second chunk alongside the code chunk | Explored (Part 4) | Better separation — code chunk for code queries, description chunk for incident queries. Not shipped: doubles chunk count |
| **Known-incidents KB** | Symptom→root-cause mappings for recurring error patterns | Shipped (incident index) | More reliable than LLM description for exact-match incident lookup |

---

### Query-side (query path)

| Approach | What it does | Status | Why |
|---|---|---|---|
| **Embed query directly** | text-embedding-3-small on raw query string | Current code | Simple, deterministic, no extra LLM call |
| **HyDE** | LLM generates a hypothetical code function, embed that instead | Explored (Part 11) | Queries 1–3: score 0.77→0.84 (better). Vocabulary-gap query: rank 3→5 (worse). LLM generates the expected function, not the actual one. Non-deterministic (different hypothetical each run). Not shipped |
| **Query rewrite** | LLM expands query with source-code vocabulary before embedding | Not shipped | HyDE is a superset — if the hypothetical fails, query rewrite would too |

---

### Retrieval

| Approach | What it does | Status | Why |
|---|---|---|---|
| **Pure vector** | cosine similarity only | Legacy (was used in agents before PR #135) | classifyFields scored 0.24 and ranked 2nd despite appearing verbatim — single identifier diluted across all tokens |
| **Hybrid search** | 0.7 × vector + 0.3 × lexical | Current code | Lexical rescues exact identifier matches. min_score=0.45 drops noise |
| **BM25 / pure lexical** | Token frequency only | Not shipped | Fails on semantic / natural-language queries |

---

### Reranking

| Approach | What it does | Status | Why |
|---|---|---|---|
| **No reranking** | Return hybrid search top-k directly | Current code for **code search** | Hybrid search with min_score is sufficient; cross-encoder adds latency |
| **Cross-encoder** (`ms-marco-MiniLM-L-6-v2`) | Full (query, doc) pair → logit score; 20 candidates → top 3 | **Shipped for incidents** (`rerank_incidents`, min_score=0.80 candidate floor) | 10.6-point CE spread (-2.31 to +8.31) vs vector's 0.033 cosine range (0.2818–0.3146) — 320× wider signal, much cleaner discrimination. O(K) cost justified for incident lookup (rare, high stakes). Not justified for every code search call |

---

### What gets passed to the LLM

| Approach | What it does | Status | Why |
|---|---|---|---|
| **Raw chunks with metadata** | file_path + start_line + end_line + chunk text | Current code | Gives the LLM exact file coordinates for tool calls (read_file, apply_edit) |
| **Context assembly / preamble** | "Retrieved K chunks from M files" header + ranked chunks | Not a separate step | Could be added; the agents construct their own tool-call context |

---

## Interview answer structure

**"Walk me through your RAG pipeline."**

Draw Diagram 1. Then:

> "Index path: function-boundary chunking for JS/TS — one function, one chunk — with a hash-check registry that skips 60–80% of embed calls on incremental re-index. The registry also handles deletion: ChromaDB upsert never deletes, so without it re-indexing accumulates ghost chunks.
>
> Query path: hybrid search — 70% vector, 30% lexical. The lexical term boost rescues exact identifier queries that pure vector buries. min_score=0.45 filters ~30% of noise before the results reach the prompt.
>
> There are two separate indexes: a code index for source retrieval and an incident index for 'have we seen this before?' lookups. The incident path uses two-stage retrieval — vector search at min_score=0.80 for recall, cross-encoder reranking for precision. The code path uses hybrid search with min_score=0.45 only."

**"What about cross-encoder reranking on code search?"**

> "It's implemented and wired to incident lookup — 10.6-point CE spread vs vector's 0.033 cosine range on the same candidate set, 320× wider signal. +8 means clearly relevant, -2 means clearly not. I wired it to incident reranking where the stakes are higher and calls are less frequent. For every code search call the latency cost wasn't justified — hybrid search with a score floor is good enough for the code retrieval case."

**"What about the vocabulary gap — runtime errors not in source code?"**

> "Explored LLM-generated descriptions appended to chunks at index time. It moved the vocabulary-gap query from not-found to rank 7 — progress, but not top-3. Also tried HyDE at query time, which helped code-vocabulary queries but made the vocabulary-gap case worse: the hypothetical function described how the error *should* be handled, not how it *actually is* handled in production code. The incident index is the more reliable solution for exact-match incident lookup — past resolved incidents are indexed by symptom text and retrieved at high threshold."

---

## Key numbers

| Number | What it is |
|---|---|
| 0.7 / 0.3 | Hybrid search weights: vector / lexical |
| 0.45 | min_score for code search — filters ~30% of naive results |
| 0.80 | min_score for incident candidate pool (vector recall stage before cross-encoder) |
| 60–80% | Hash check skip rate on incremental re-index |
| 0.56 → 0.71 | Similarity score gain from function-boundary chunking |
| rank 3 → 1 | Rank improvement from function-boundary chunking |
| 10.6 pts / 0.033 | Cross-encoder score range (-2.31 to +8.31) vs vector range (0.2818–0.3146) — 320× wider signal |
| rank 7 | Where LLM description placed the vocabulary-gap query (not good enough) |
| rank 3 → 5 | HyDE made the vocabulary-gap query *worse* |

---

## Common Probes

**"What if retrieval returns nothing?"** Surface it explicitly rather than silently passing empty context to the LLM. Options: (1) widen min_score threshold or drop it entirely, (2) fall back to BM25 / exact grep, (3) return "I couldn't find relevant context" with a prompt constraint against hallucinating. In my system the diagnosis confidence gate (0.70 threshold) blocks the pipeline if retrieval is too weak to form a grounded answer.

**"How do you handle vocabulary gap — runtime errors not in source code?"** `NoSuchKey` doesn't appear anywhere in the S3 handler. Three mitigations: (1) LLM-augmented chunk descriptions at index time — bridges runtime vocabulary to source vocabulary (explored, moved vocab-gap query from not-found to rank 7, not shipped — dilution tradeoff); (2) known-incidents knowledge base with symptom→root-cause mappings (shipped — incident index); (3) accept RAG as a first-pass filter and fall back to full-file retrieval for fix generation.

**"How do you evaluate RAG quality?"** Recall@3: given a known (query, ground-truth chunk) pair, did the correct chunk appear in the top 3? Build a golden dataset of 8–20 pairs covering normal cases, vocabulary-gap cases, and identifier cases. Baseline first. LLM-as-judge for faithfulness and relevance. Alert when faithfulness drops below 0.7.

**"Shadow index / zero-downtime rebuild?"** Build new index in `codebase_v2` while live queries hit `codebase_v1`. Validate shadow against benchmark queries. Swap atomically via config pointer. Keep `codebase_v1` for 24–48h rollback window.

**"Why two separate collections in DiagnosisAgent?"** Different retrieval semantics. The incident index matches on symptom patterns — short natural-language descriptions of error behavior. The code index matches on code structure and identifiers. Combining them forces a single embedding space to represent both, which degrades retrieval quality on both. Separation lets each index be tuned independently: different chunking strategies, different score thresholds (0.45 vs 0.90).

---

## Failure Modes to Close With

1. **Ghost chunks** — re-index without delete leaves superseded chunks that surface in queries. The chunk registry + explicit delete-before-insert is the fix.
2. **Stale metadata in the index** — always confirm live state from the authoritative store, not ChromaDB metadata. The vector store is a candidate finder, not a source of truth.
3. **Score calibration mismatch** — code search scores are 30–40% lower than prose search; don't reuse the same min_score threshold across domains.
4. **Model mismatch** — different embedding models at index and query time produce meaningless similarity scores; silent failure, no error thrown.
5. **Retrieve-Everything** — passing all n_results to the LLM regardless of score floods the prompt with noise. min_score=0.45 is the fix; the Retrieve-Everything anti-pattern was an active bug in this system before the upgrade.
6. **Context overflow** — unlimited top-k eventually exceeds the prompt window; always cap and rank by score.
