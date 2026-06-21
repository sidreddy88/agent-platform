# AI Design Patterns in Production

*Interview reference doc. Maps every pattern from ai-system-design-guide/15-ai-design-patterns against the agent-platform. One code change made: upgraded DiagnosisAgent and FixGenerationAgent from naive semantic search to hybrid_search with min_score=0.45.*

---

## The pattern map

| Pattern | Status in agent-platform |
|---|---|
| **Naive RAG** | Was used — upgraded |
| **Advanced RAG (hybrid search)** | Implemented in `rag.py`; now wired into agents |
| **ReAct** | Core of `BaseAgent` |
| **Critic/Verifier** | `CodeReviewAgent` (GPT-4.1 reviews Sonnet) + `SelfCritiqueAgent` |
| **Hierarchical Agents** | Triage → Diagnosis → FixGen → CodeReview → SelfCritique |
| **Cascading Models** | Haiku (triage/critique), Sonnet (reasoning), GPT-4.1 (judge) |
| **Circuit Breaker** | `app/services/circuit_breaker.py` |
| **Bulkhead** | MasterOrchestrator priority-lane semaphores (P0: 2, P1: 4, P2/P3: 6) |
| **Token Budget** | Context checkpoint + state pruning in `BaseAgent` and FixGen loop |
| **Cost Tracking** | `LLMGateway` logs cost per call, `_daily_costs` by task type |

---

## RAG Patterns

### Where the platform sits on the RAG spectrum

```
Naive RAG:     embed → search top-K → generate
Advanced RAG:  query-rewrite → hybrid search → rerank → filter → generate
Parent-Child:  small chunks for retrieval, large chunks for generation
Self-RAG:      model decides when to retrieve
CRAG:          grade retrieved docs; fall back to web search if poor
```

The agent-platform runs **Advanced RAG** minus query rewriting. `RAGService.hybrid_search()` combines:

```
hybrid_score = 0.7 × vector_cosine_similarity + 0.3 × lexical_token_match
```

This is particularly effective for code search because:
- Vector captures semantic meaning ("authentication error" → finds auth middleware)
- Lexical rewards exact token presence (`TypeError`, `undefined`, function names)

Function-boundary chunking is used for JS/TS files — each top-level function becomes one chunk instead of fixed-line splits. Python files use 50-line chunks with 10-line overlap.

### The code fix: upgrading from naive to hybrid

Both agents were calling `rag.search()` (pure vector, no score floor), even though `hybrid_search()` was implemented and the docstring recommended `min_score=0.45` for code search.

```python
# Before (naive — pure vector, no quality floor)
chunks = await rag.search(query, n_results=4)

# After (Advanced RAG — hybrid, noise filtered below 0.45)
chunks = await rag.hybrid_search(query, n_results=4, min_score=0.45)
```

Applied in both `diagnosis.py` (`_search_codebase` tool) and `fix_generation.py` (critique context builder). The `min_score=0.45` filter drops low-relevance chunks that would otherwise add noise to the prompt — this is the "Quality over quantity" anti-pattern fix from the guide.

**What the Retrieve-Everything anti-pattern costs:** sending 4 low-relevance chunks to the diagnosis LLM adds ~400 tokens of context noise per chunk. At `min_score=0.45`, roughly 30% of naive results are filtered, tightening the context window and reducing hallucination risk.

### What's not implemented (and why)

| Pattern | Why not |
|---|---|
| **Parent-Child Retrieval** | Would require double-write at index time; 50-line chunks with 10-line overlap approximate the benefit cheaply |
| **Self-RAG** | Adds 2–3 LLM calls per search decision; at O(10) incidents/day the cost isn't justified |
| **CRAG** | Requires a relevance grader LLM + web search fallback; justified when the indexed codebase is stale or incomplete |

---

## Agent Patterns

### ReAct — the foundation

Every agent extends `BaseAgent`, which implements:

```
Thought → Action → Observation → (repeat up to MAX_ITERATIONS=10) → Answer
```

`MAX_ITERATIONS = 10` is the hard termination guard. The Infinite Loop anti-pattern is mitigated: if the agent hasn't reached a conclusion in 10 steps, it returns the best answer accumulated so far. A per-iteration context checkpoint compresses the message history when token count approaches the context limit.

**Why ReAct for incident diagnosis:** the Thought step forces the model to reason about what information it needs before calling tools, which reduces tool flailing. The Observation step gives the model grounding after each tool call — it can't skip ahead to a conclusion without actually reading the output.

### Critic/Verifier — two implementations

**1. Cross-model code review (GPT-4.1 reviews Sonnet):**
The same-model judge problem: if Sonnet reviews its own fix, it's biased toward approving its own output. Using a different model family (GPT-4.1) for evaluation removes this bias. The guide calls this the PoLL pattern — here it's a panel of one, but different family is the key insight.

**2. SelfCritiqueAgent:**
Haiku reviews the Sonnet fix against a structured checklist before the PR is opened. It's fast (Haiku = cheap) and catches formatting issues and missing tests — the cases where the generator systematically misses things the checklist would catch.

### Hierarchical Agents

```
TriageAgent (Haiku)         → severity + urgency classification
DiagnosisAgent (Sonnet)     → root cause identification
FixGenerationAgent (Sonnet) → PR creation
CodeReviewAgent (GPT-4.1)   → cross-model fix validation
SelfCritiqueAgent (Haiku)   → checklist compliance
```

This is a strict linear hierarchy, not a parallel manager-worker setup. The value: each agent has a focused prompt and the right model for its job. TriageAgent doesn't need Sonnet-level reasoning — Haiku classifying severity is 10× cheaper and equally accurate.

### What's not implemented

**Plan-and-Execute:** The DiagnosisAgent doesn't explicitly create a plan before acting. ReAct is sufficient because the task is bounded (one incident, one codebase). Plan-and-Execute becomes justified for open-ended tasks like "migrate this service to a new framework" where the number of steps isn't known in advance.

---

## Optimization Patterns

### Cascading Models

The platform implements model cascading by design, not dynamically. Rather than routing based on query complexity, each pipeline stage is statically assigned the right model:

| Stage | Model | Rationale |
|---|---|---|
| Triage + self-critique | Haiku | Classification + checklist, no reasoning required |
| Diagnosis + fix gen | Sonnet | Multi-step reasoning, code understanding |
| Code review | GPT-4.1 | Independent judge, different model family |

Dynamic routing (classify complexity → choose model) would add an LLM call for the classifier. Static assignment is fine when tasks are homogeneous within each stage.

### Prompt Caching

`BaseAgent` marks harness docs (AGENTS.md, CONSTRAINTS.md) as `cache_control: ephemeral`, making them the stable prefix that Anthropic caches across calls. Cache tokens bill at 10% of input price. Average diagnosis cost: **$0.1861/incident** including all pipeline stages.

### State Pruning (Token Budget)

The `BaseAgent` context checkpoint fires when accumulated tokens approach the context limit. Stale tool results are pruned from the message history before compression — the most recent observations survive, old ones are replaced with a summary marker. This prevents context overflow on incidents that require many tool calls.

---

## Reliability Patterns

### Circuit Breaker (fully implemented)

`app/services/circuit_breaker.py` implements the three-state machine on two services:
- `"anthropic_llm"` — wraps every LLM call (5 failures → OPEN, 60s timeout before HALF_OPEN)
- `"github_api"` — wraps GitHub API calls

Distinguishes circuit breaker from retry: retry handles *transient* failures (one bad call); circuit breaker handles *systemic* failures (provider down). Without the circuit breaker, a provider outage burns 3 retry attempts × 30s each before failing — the system looks hung. With it, after 5 failures everything fails in <1ms and routes to human escalation.

### Bulkhead Isolation (fully implemented)

`MasterOrchestrator` uses per-priority-lane semaphores — completely independent, no cross-lane starvation:

```
P0 (critical): Semaphore(2)   — max 2 concurrent
P1 (high):     Semaphore(4)
P2/P3 (low):   Semaphore(6)
```

A storm of low-priority incidents can't starve critical incidents. This is the bulkhead pattern: isolate failures (or overload) between components. Without it, 6 concurrent P3 incidents could block a P0 incident from getting a worker.

### What's not fully implemented

**Multi-provider failover:** If Anthropic goes down, DiagnosisAgent and FixGenerationAgent fail — the circuit opens and incidents escalate to human. If OpenAI goes down, code review is skipped. True failover (run DiagnosisAgent on GPT-4.1 when Anthropic is unavailable) would require provider-agnostic prompt engineering. Current prompts include Anthropic-specific formatting that can't be assumed to work identically on GPT-4.1. This is a deliberate trade-off: cross-provider consistency is harder to maintain than accepting the escalation path.

---

## Anti-Patterns: What the Platform Avoids

### God Prompt — avoided

Each agent has a focused system prompt for one task. The guide's example of a 5000-token God Prompt trying to handle customer support, refunds, scheduling, and code generation in one prompt is the opposite of what's here. TriageAgent has a triage prompt; DiagnosisAgent has a diagnosis prompt. Updates to one don't affect others.

### Retrieve Everything — fixed

The pre-fix code passed all `n_results=4` chunks to the LLM regardless of relevance score. The `min_score=0.45` filter now drops noise. The "Lost in the middle" effect is real: relevant chunks buried in a longer context are overlooked. Passing 4 tight, relevant chunks beats passing 4 chunks plus 3 irrelevant ones.

### Unsafe Tool Access — mitigated

DiagnosisAgent has read-only tools (search, file read). FixGenerationAgent has write tools (`apply_edit`, `patch_line`, `create_file`) but they're gated behind the approval system: HIGH/CRITICAL-rated changes require human sign-off before the GitHub write executes. The OWASP LLM Top 10 #8 (Excessive Agency) is mitigated here.

### Infinite Loop Risk — mitigated

`MAX_ITERATIONS = 10` in `BaseAgent`. The FixGenerationAgent's agentic loop has its own per-step counter. Both return a partial answer if the limit is hit rather than running forever.

### Anti-patterns that apply

| Anti-pattern | Status |
|---|---|
| No rate limiting on API | **Present** — single-tenant platform, not SaaS, so not critical yet |
| Single provider dependency | **Partial** — Anthropic + OpenAI; no Google/Gemini fallback |
| No semantic caching | **Present** — prompt caching is implemented; semantic cache (same FAQ → same answer) is not |

---

## Interview Q&A

**"What RAG pattern does your pipeline use?"**

> "Advanced RAG with hybrid search — combining 70% vector cosine similarity with 30% lexical token matching. Code search specifically benefits from the lexical component: if the query contains `TypeError` or a function name, the lexical term match rescues chunks that pure vector search would rank lower.
>
> Chunking is semantic-aware: JS/TS files chunk at function boundaries; Python uses 50-line chunks with 10-line overlap. A `min_score=0.45` filter drops low-relevance results before they reach the prompt — the Retrieve-Everything anti-pattern was explicitly fixed: previously the agents used pure semantic search with no score floor.
>
> What I don't do: parent-child retrieval (approximated by overlap), Self-RAG (too expensive at current volume), or CRAG (not needed when the codebase is a controlled corpus with known freshness)."

**"How do you prevent agents from running forever?"**

> "Three layers. First, `MAX_ITERATIONS=10` in `BaseAgent` — the ReAct loop exits with the best partial answer after 10 steps. Second, the circuit breaker: if LLM calls are failing, the circuit opens and the loop fails fast rather than burning through retries. Third, the bulkhead: priority-lane semaphores cap concurrent runs per priority level, so a stuck incident can't prevent others from starting.
>
> The cost termination (kill after $X) from the guide isn't implemented — at $0.18/incident average and O(10) incidents/day, a soft cost gate isn't necessary yet. The circuit breaker is the practical equivalent for the budget case: if costs spike due to repeated retries, the breaker opens before it escalates."

**"What's the difference between a Critic/Verifier and an ensemble?"**

> "A Critic/Verifier is sequential: generate → critique → optionally regenerate. One thread of execution, quality check in the middle. An ensemble is parallel: generate N candidates → aggregate or select the best.
>
> In the pipeline, CodeReviewAgent is a Critic/Verifier — GPT-4.1 reviews Sonnet's fix after generation, before the PR opens. It can block or approve the fix. If it were an ensemble, I'd run Sonnet and GPT-4.1 in parallel both generating fixes, then synthesize.
>
> The Critic/Verifier costs 1× generation + 1× evaluation. MoA-style parallel generation costs 2× generation + synthesis. At $0.18/incident, the Critic/Verifier gives the cross-model bias protection without doubling the fix generation cost."

**"Which reliability pattern is most important for LLM applications?"**

> "Circuit breaker. It's the one with no equivalent in traditional service reliability — or rather, traditional systems usually recover fast enough that it doesn't matter. LLM APIs fail slowly: a timeout burns 30 seconds, and retries compound that. During an Anthropic outage without circuit breaker, every incident in the queue burns 3 × 30s = 90s before failing. With the circuit breaker, after 5 failures the breaker opens and everything after fails in <1ms. The system stays responsive and routes to human escalation instead of appearing hung.
>
> Bulkhead isolation is the second most important. It's what prevents a flood of low-priority events from blocking critical incident processing — the priority semaphores are the production equivalent of thread pool isolation."

---

## Key numbers to know cold

- `hybrid_search` alpha: **0.7** vector + **0.3** lexical — keeps semantic as primary signal
- `min_score` recommended for code: **0.45** — filters ~30% of low-relevance naive results
- `MAX_ITERATIONS`: **10** — hard termination guard in BaseAgent
- Circuit breaker: **5 failures** → OPEN, **60s** before HALF_OPEN
- Priority lanes: **P0=2, P1=4, P2/P3=6** concurrent — independent semaphores, no cross-lane starvation
- Average pipeline cost: **$0.1861/incident** (with prompt caching)
- Token Budget: context checkpoint fires approaching context limit; stale observations pruned first
