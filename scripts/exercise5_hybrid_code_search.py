"""
Exercise 5: Hybrid search on the enriched function-boundary collection.

Exercise 4 pushed "S3 NoSuchKey missing key error" to rank 7, score 0.28.
The description now contains "NoSuchKey" literally — but vector similarity alone
doesn't reward exact token presence enough.

Hybrid formula (same as incident RAG Part 6):
  hybrid = alpha * vector_score + (1 - alpha) * lexical_score
  lexical_score = fraction of query tokens found in the document text

"NoSuchKey" appears in the enriched chunk → lexical = 1.0 for that token.
The lexical bonus should push the function well into top-3.

Runs a four-way comparison:
  line-based vector only (Exercise 2 baseline)
  function-boundary vector only (Exercise 3)
  enriched vector only (Exercise 4)
  enriched hybrid (Exercise 5)

Usage:
    python scripts/exercise5_hybrid_code_search.py
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

ALPHA = 0.7   # weight for vector score; (1-alpha) for lexical

RESET  = "\033[0m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
GREY   = "\033[90m"


def hybrid_score(vector: float, document: str, query: str, alpha: float = ALPHA) -> float:
    tokens = set(query.lower().split())
    doc_lower = document.lower()
    matched = sum(1 for t in tokens if t in doc_lower)
    lexical = matched / len(tokens) if tokens else 0.0
    return alpha * vector + (1 - alpha) * lexical, lexical


async def hybrid_search(rag, collection, query: str, candidate_pool: int = 20):
    """Fetch wide candidate set, re-score with hybrid formula, return sorted."""
    embedding = await rag._embed([query])
    candidates = collection.query(embedding[0], n_results=candidate_pool)

    scored = []
    for m in candidates:
        h, lex = hybrid_score(m.score, m.document, query)
        scored.append((h, m.score, lex, m))

    scored.sort(key=lambda x: x[0], reverse=True)
    return scored


async def search_collection(rag, collection, query: str, n: int = 10):
    embedding = await rag._embed([query])
    return collection.query(embedding[0], n_results=n)


async def main():
    from dotenv import load_dotenv
    load_dotenv()

    from app.services.rag import RAGService
    from app.services.vector_store import make_collection

    rag              = RAGService()
    fn_collection    = make_collection("codebase_fn")
    enriched_col     = make_collection("codebase_fn_enriched")

    if enriched_col.count() == 0:
        print("Run exercise4_enriched_chunks.py first to build the enriched collection.")
        return

    print(f"{BOLD}Four-way comparison — target: {TARGET_FN}{RESET}\n")
    header = f"{'Query':<43}  {'Line':^14}  {'Fn-bndry':^14}  {'Enrichd':^14}  {'Hybrid':^14}"
    print(header)
    print(f"{'':43}  {'r  score':^14}  {'r  score':^14}  {'r  score':^14}  {'r  score':^14}")
    print("─" * 115)

    for query in QUERIES:
        # ── Line-based vector ──────────────────────────────────────────
        line_results = await rag.search(query, n_results=10)
        lr, ls = "–", "–"
        for i, r in enumerate(line_results, 1):
            if r.start_line in TARGET_LINE_CHUNKS:
                lr, ls = str(i), f"{r.score:.3f}"
                break

        # ── Function-boundary vector ───────────────────────────────────
        fn_results = await search_collection(rag, fn_collection, query)
        fr, fs = "–", "–"
        for i, m in enumerate(fn_results, 1):
            if m.metadata.get("function_name") == TARGET_FN:
                fr, fs = str(i), f"{m.score:.3f}"
                break

        # ── Enriched vector only ───────────────────────────────────────
        en_results = await search_collection(rag, enriched_col, query)
        er, es = "–", "–"
        for i, m in enumerate(en_results, 1):
            if m.metadata.get("function_name") == TARGET_FN:
                er, es = str(i), f"{m.score:.3f}"
                break

        # ── Enriched hybrid ────────────────────────────────────────────
        hy_results = await hybrid_search(rag, enriched_col, query)
        hr, hs, hlex = "–", "–", 0.0
        for i, (h, vec, lex, m) in enumerate(hy_results, 1):
            if m.metadata.get("function_name") == TARGET_FN:
                hr, hs, hlex = str(i), f"{h:.3f}", lex
                break

        # Colour hybrid green if it improved over enriched vector
        en_num = int(er) if er != "–" else 99
        hy_num = int(hr) if hr != "–" else 99
        color = GREEN if hy_num < en_num else (YELLOW if hy_num == en_num else RESET)

        print(
            f"  {query:<41}  "
            f"{'r'+lr+' '+ls:^14}  "
            f"{'r'+fr+' '+fs:^14}  "
            f"{'r'+er+' '+es:^14}  "
            f"{color}{'r'+hr+' '+hs:^14}{RESET}"
        )

    # ── Detail view for the vocabulary gap query ───────────────────────
    print(f"\n{CYAN}Detail: 'S3 NoSuchKey missing key error' — top 5 hybrid results{RESET}\n")
    hy_results = await hybrid_search(rag, enriched_col, "S3 NoSuchKey missing key error")
    for i, (h, vec, lex, m) in enumerate(hy_results[:5], 1):
        name = m.metadata.get("function_name", "?")
        marker = " ◀ TARGET" if name == TARGET_FN else ""
        print(f"  rank {i}  hybrid={h:.4f}  (vec={vec:.4f}  lex={lex:.2f})  {name}{marker}")
        # Show last line of document (the appended description)
        last_line = [l for l in m.document.splitlines() if l.strip()][-1]
        print(f"         {GREY}{last_line[:90]}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
