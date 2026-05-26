"""
Exercise 6: HyDE (Hypothetical Document Embeddings) on the enriched collection.

Exercise 5 pushed "S3 NoSuchKey missing key error" to rank 1 via hybrid search.
HyDE is a complementary query-time technique: instead of embedding the raw query,
generate a hypothetical function that would match the query, then embed that instead.

The vocabulary gap problem in a different form:
  - Query: "S3 NoSuchKey missing key error"  ← incident vocabulary
  - Index: copyObject, deleteObject, bucket   ← code vocabulary
  HyDE bridges the gap at query time by generating code-vocabulary text from
  the incident-vocabulary query before embedding.

Runs a five-way comparison:
  line-based vector only (Exercise 2 baseline)
  function-boundary vector only (Exercise 3)
  enriched vector only (Exercise 4)
  enriched hybrid (Exercise 5)
  HyDE on enriched collection (Exercise 6)

Usage:
    python scripts/exercise6_hyde.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TARGET_FN          = "moveAndRemoveFileFromS3"
TARGET_LINE_CHUNKS = {441, 481}

QUERIES = [
    "moveAndRemoveFileFromS3",
    "copy S3 object then delete source",
    "S3 copyObject deleteObject bucket key",
    "S3 NoSuchKey missing key error",
]

ALPHA = 0.7

RESET  = "\033[0m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
GREY   = "\033[90m"

HYDE_PROMPT = """\
A developer is searching a JavaScript codebase for: "{query}"

Write a realistic JavaScript function (10-25 lines) that they are likely looking for.
Use real AWS SDK / Node.js patterns. Output only the code, no explanation."""


async def generate_hypothetical(client, query: str) -> str:
    """Generate a hypothetical JS function for the given query using Claude Haiku."""
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=300,
        messages=[{
            "role": "user",
            "content": HYDE_PROMPT.format(query=query),
        }],
    )
    return msg.content[0].text.strip()


def hybrid_score(vector: float, document: str, query: str, alpha: float = ALPHA) -> float:
    tokens = set(query.lower().split())
    doc_lower = document.lower()
    matched = sum(1 for t in tokens if t in doc_lower)
    lexical = matched / len(tokens) if tokens else 0.0
    return alpha * vector + (1 - alpha) * lexical, lexical


async def hybrid_search(rag, collection, query: str, candidate_pool: int = 20):
    embedding = await rag._embed([query])
    candidates = collection.query(embedding[0], n_results=candidate_pool)
    scored = []
    for m in candidates:
        h, lex = hybrid_score(m.score, m.document, query)
        scored.append((h, m.score, lex, m))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


async def hyde_search(rag, client, collection, query: str, n: int = 20):
    """Embed a hypothetical document instead of the raw query."""
    hypothetical = await generate_hypothetical(client, query)
    embedding = await rag._embed([hypothetical])
    results = collection.query(embedding[0], n_results=n)
    return results, hypothetical


async def search_collection(rag, collection, query: str, n: int = 10):
    embedding = await rag._embed([query])
    return collection.query(embedding[0], n_results=n)


async def main():
    from dotenv import load_dotenv
    load_dotenv()

    import anthropic
    from app.services.rag import RAGService
    from app.services.vector_store import make_collection

    rag              = RAGService()
    anthropic_client = anthropic.AsyncAnthropic()
    fn_collection    = make_collection("codebase_fn")
    enriched_col     = make_collection("codebase_fn_enriched")

    if enriched_col.count() == 0:
        print("Run exercise4_enriched_chunks.py first to build the enriched collection.")
        return

    print(f"{BOLD}Five-way comparison — target: {TARGET_FN}{RESET}\n")
    header = (
        f"{'Query':<43}  {'Line':^13}  {'Fn-bndry':^13}  "
        f"{'Enrichd':^13}  {'Hybrid':^13}  {'HyDE':^13}"
    )
    print(header)
    print(f"{'':43}  {'r  score':^13}  {'r  score':^13}  {'r  score':^13}  {'r  score':^13}  {'r  score':^13}")
    print("─" * 130)

    hypotheticals = {}

    for query in QUERIES:
        # ── Line-based vector ──────────────────────────────────────────────
        line_results = await rag.search(query, n_results=10)
        lr, ls = "–", "–"
        for i, r in enumerate(line_results, 1):
            if r.start_line in TARGET_LINE_CHUNKS:
                lr, ls = str(i), f"{r.score:.3f}"
                break

        # ── Function-boundary vector ───────────────────────────────────────
        fn_results = await search_collection(rag, fn_collection, query)
        fr, fs = "–", "–"
        for i, m in enumerate(fn_results, 1):
            if m.metadata.get("function_name") == TARGET_FN:
                fr, fs = str(i), f"{m.score:.3f}"
                break

        # ── Enriched vector only ───────────────────────────────────────────
        en_results = await search_collection(rag, enriched_col, query)
        er, es = "–", "–"
        for i, m in enumerate(en_results, 1):
            if m.metadata.get("function_name") == TARGET_FN:
                er, es = str(i), f"{m.score:.3f}"
                break

        # ── Enriched hybrid ────────────────────────────────────────────────
        hy_results = await hybrid_search(rag, enriched_col, query)
        hr, hs = "–", "–"
        for i, (h, vec, lex, m) in enumerate(hy_results, 1):
            if m.metadata.get("function_name") == TARGET_FN:
                hr, hs = str(i), f"{h:.3f}"
                break

        # ── HyDE ──────────────────────────────────────────────────────────
        hyde_results, hyp_text = await hyde_search(rag, anthropic_client, enriched_col, query)
        hypotheticals[query] = hyp_text
        yr, ys = "–", "–"
        for i, m in enumerate(hyde_results, 1):
            if m.metadata.get("function_name") == TARGET_FN:
                yr, ys = str(i), f"{m.score:.3f}"
                break

        # Colour HyDE: green if rank improved over hybrid, yellow if same, red if worse
        hy_num = int(hr) if hr != "–" else 99
        hy_num_hyde = int(yr) if yr != "–" else 99
        if hy_num_hyde < hy_num:
            hyde_color = GREEN
        elif hy_num_hyde == hy_num:
            hyde_color = YELLOW
        else:
            hyde_color = RESET

        print(
            f"  {query:<41}  "
            f"{'r'+lr+' '+ls:^13}  "
            f"{'r'+fr+' '+fs:^13}  "
            f"{'r'+er+' '+es:^13}  "
            f"{'r'+hr+' '+hs:^13}  "
            f"{hyde_color}{'r'+yr+' '+ys:^13}{RESET}"
        )

    # ── Detail: show hypothetical functions for each query ─────────────────
    print(f"\n{CYAN}Hypothetical functions generated by HyDE:{RESET}\n")
    for query in QUERIES:
        print(f"  {BOLD}Query:{RESET} {query}")
        for line in hypotheticals[query].splitlines():
            print(f"  {GREY}{line}{RESET}")
        print()

    # ── Detail: top-5 for the vocabulary gap query ─────────────────────────
    print(f"{CYAN}Detail: 'S3 NoSuchKey missing key error' — top 5 HyDE results{RESET}\n")
    hyde_results, _ = await hyde_search(rag, anthropic_client, enriched_col, "S3 NoSuchKey missing key error")
    for i, m in enumerate(hyde_results[:5], 1):
        name = m.metadata.get("function_name", "?")
        marker = " ◀ TARGET" if name == TARGET_FN else ""
        print(f"  rank {i}  score={m.score:.4f}  {name}{marker}")
        last_line = [l for l in m.document.splitlines() if l.strip()][-1]
        print(f"         {GREY}{last_line[:90]}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
