# Call Graph Data Structures — Interview Reference

Five structures for representing a call graph, with time/space complexity, trade-offs, and when to use each.

**Variables used throughout:**
- **V** = number of functions (vertices)
- **E** = number of call edges
- **d** = degree of a node (number of direct callers/callees for a specific function)

---

## 1. Adjacency List

**What it is:** A dict mapping each function to a list of its neighbors. For a call graph you maintain two: forward (callees) and reverse (callers).

```python
forward = {
    "classifyFields": ["openai.complete", "validateSchema"],
    "insertMany":     ["classifyFields", "db.collection"],
    "saveRecord":     ["classifyFields", "insertMany"],
}

reverse = {
    "classifyFields": ["insertMany", "saveRecord"],
    "validateSchema": ["classifyFields"],
    "openai.complete": ["classifyFields"],
}
```

**Time complexity**

| Operation | Time |
|---|---|
| Build | O(V + E) |
| Lookup all callers of X | O(1) dict hit + O(d) to return list |
| Lookup all callees of X | O(1) dict hit + O(d) to return list |
| Check if A calls B | O(d) — scan A's callee list |
| Transitive callers (BFS) | O(V + E) |

**Space complexity:** O(V + E) — stores exactly the edges that exist, nothing more.

**When to use:** Default choice for sparse graphs. Codebases are sparse — a function calls 5-15 others on average, not thousands. Adjacency list wastes no memory on non-existent edges.

**Weakness:** "Does A call B?" is O(d), not O(1). Use a hash map of sets (Structure 3) to fix this.

---

## 2. Adjacency Matrix

**What it is:** An N×N boolean grid where `matrix[i][j] = True` if function i calls function j. Rows = callers, columns = callees.

```python
# Functions: [classifyFields=0, insertMany=1, saveRecord=2, validateSchema=3]
matrix = [
    #  cF     iM     sR     vS
    [False, False, False, True ],  # classifyFields calls validateSchema
    [True,  False, False, False],  # insertMany calls classifyFields
    [True,  True,  False, False],  # saveRecord calls classifyFields + insertMany
    [False, False, False, False],  # validateSchema calls nothing
]
```

**Time complexity**

| Operation | Time |
|---|---|
| Build | O(V²) |
| Check if A calls B | O(1) — direct index |
| Find all callees of A | O(V) — scan row A |
| Find all callers of B | O(V) — scan column B |
| Transitive callers (BFS) | O(V²) |

**Space complexity:** O(V²) — always, regardless of how many edges exist.

**When to use:** Only when the graph is dense (E approaches V²) and O(1) edge existence checks are critical. Almost never the right choice for codebases.

**Weakness:** 10K functions = 100M matrix entries. At ~1 byte each, that's 100MB for a moderately sized codebase, 99%+ of it storing `False`. Prohibitively wasteful for sparse graphs.

---

## 3. Hash Map of Sets

**What it is:** Same shape as adjacency list but the inner container is a `set` instead of a `list`. Trades insertion-order for O(1) existence checks.

```python
forward = {
    "classifyFields": {"openai.complete", "validateSchema"},
    "insertMany":     {"classifyFields", "db.collection"},
    "saveRecord":     {"classifyFields", "insertMany"},
}

reverse = {
    "classifyFields": {"insertMany", "saveRecord"},
}
```

**Time complexity**

| Operation | Time |
|---|---|
| Build | O(V + E) |
| Lookup all callers of X | O(1) dict hit + O(d) to return set |
| Check if A calls B | O(1) — set membership |
| Add a new edge | O(1) amortized |
| Remove an edge | O(1) |

**Space complexity:** O(V + E) — same as adjacency list, slight overhead per set object.

**When to use:** When you need fast edge existence checks (`does A call B?`) AND fast neighbor enumeration. This is the production default for in-memory call graphs — it strictly dominates the adjacency list for most query patterns.

**Weakness:** Sets are unordered. If you need callers in a deterministic order (e.g., for stable test output or reproducible prompts), sort at query time: `sorted(reverse[fn])`.

---

## 4. Edge List

**What it is:** A flat list of `(caller, callee)` tuples. The simplest possible representation.

```python
edges = [
    ("insertMany",  "classifyFields"),
    ("saveRecord",  "classifyFields"),
    ("saveRecord",  "insertMany"),
    ("classifyFields", "validateSchema"),
]
```

**Time complexity**

| Operation | Time |
|---|---|
| Build | O(E) |
| Find all callers of X | O(E) — full scan |
| Check if A calls B | O(E) — full scan |
| Add a new edge | O(1) — append |

**Space complexity:** O(E) — only edges, no per-vertex overhead.

**When to use:** Serialization (writing to disk, sending over the wire). Building other structures from scratch. Never as the primary query structure.

**Weakness:** Every query is O(E). Acceptable for a 50-edge graph, unusable for a 500K-edge production codebase.

---

## 5. Database Table with B-tree Index (Postgres)

**What it is:** A relational table persisting call edges, with a B-tree index on the `callee_id` column enabling fast reverse lookup. This is Phase 5 of the code graph plan.

```sql
-- Schema
CREATE TABLE code_call_sites (
    id          SERIAL PRIMARY KEY,
    caller_id   INTEGER REFERENCES code_symbols(id),
    callee_name TEXT NOT NULL,
    callee_id   INTEGER REFERENCES code_symbols(id),
    line        INTEGER,
    resolution  TEXT  -- 'resolved' | 'unresolved' | 'ambiguous'
);

-- Index that makes reverse lookup fast
CREATE INDEX idx_call_sites_callee ON code_call_sites(callee_id);
CREATE INDEX idx_call_sites_caller ON code_call_sites(caller_id);
```

**Time complexity**

| Operation | Time |
|---|---|
| Build (bulk insert) | O(E log E) — insert + index maintenance |
| Find all callers of X | O(log V + d) — B-tree scan + result fetch |
| Check if A calls B | O(log E) — index lookup |
| Incremental update (one file changed) | O(d log E) — delete old edges, insert new |
| Transitive callers (recursive CTE) | O(V + E) |

**Space complexity:** O(V + E) on disk + O(V log V) for the B-tree index pages.

```sql
-- Transitive callers in one query (recursive CTE)
WITH RECURSIVE callers AS (
    SELECT caller_id FROM code_call_sites WHERE callee_id = $1
    UNION
    SELECT cs.caller_id FROM code_call_sites cs
    JOIN callers c ON cs.callee_id = c.caller_id
)
SELECT * FROM callers;
```

**When to use:** Production persistence layer. Survives restarts, queryable with SQL, supports incremental updates when files change, handles transitive traversal natively via recursive CTEs.

**Weakness:** Disk I/O adds latency. The in-memory structures (1-3) are the query path in production; Postgres is the source of truth that rebuilds them on startup.

---

## Summary Table

| Structure | Build | Caller Lookup | Edge Check | Space | Best For |
|---|---|---|---|---|---|
| Adjacency List | O(V+E) | O(d) | O(d) | O(V+E) | Default in-memory choice |
| Adjacency Matrix | O(V²) | O(V) | O(1) | O(V²) | Dense graphs only |
| Hash Map of Sets | O(V+E) | O(d) | O(1) | O(V+E) | Production in-memory |
| Edge List | O(E) | O(E) | O(E) | O(E) | Serialization only |
| Postgres + B-tree | O(E log E) | O(log V + d) | O(log E) | O(V+E) disk | Persistence layer |

---

## What This Codebase Uses

**In-memory query path:** Hash Map of Sets (Structure 3) for the forward and reverse indices — O(1) edge checks, O(d) neighbor enumeration, O(V+E) space.

**Persistence (Phase 5):** Postgres table with B-tree index on `callee_id` (Structure 5) — survives restarts, supports incremental re-indexing when files change via content hash comparison.

**The in-memory graph is rebuilt from Postgres on startup.** Queries never hit disk at runtime.

---

## Interview One-Liner

> "In-memory we use a hash map of sets — forward and reverse — so both caller lookup and edge existence checks are O(1). For persistence we use a Postgres table with a B-tree index on the callee column, giving O(log n) disk lookup and surviving restarts. The in-memory graph is the hot query path; Postgres is the source of truth."
