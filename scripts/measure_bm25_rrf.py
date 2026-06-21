"""
Measure retrieval quality: pure vector vs hybrid (score addition) vs BM25+RRF.

Runs the 4 canonical queries from the Code RAG series through all three
configurations and prints rank + score for each.

Usage:
    python scripts/measure_bm25_rrf.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.rag import RAGService

QUERIES = [
    ("moveAndRemoveFileFromS3",              "identifier"),
    ("copy S3 object then delete source",    "semantic"),
    ("S3 copyObject deleteObject bucket key","mixed"),
    ("S3 NoSuchKey missing key error",       "vocab-gap"),
    ("classifyFields",                       "identifier"),
]

N_RESULTS = 10  # retrieve top-10 so we can see where ground truth lands


async def run():
    rag = RAGService()
    count = rag._collection.count()
    print(f"Index: {count} chunks\n")
    if count == 0:
        print("ERROR: no chunks indexed. Run index_codebase() first.")
        return

    for query, qtype in QUERIES:
        print(f"{'─'*70}")
        print(f"Query : {query!r}  [{qtype}]")
        print()

        # ── 1. Pure vector ────────────────────────────────────────────────
        embedding = await rag._embed([query])
        raw = rag._collection.query(embedding[0], n_results=N_RESULTS)
        print(f"  {'PURE VECTOR':30s}  rank  score    file:line")
        for i, m in enumerate(raw):
            fp = m.metadata.get("file_path", "?").split("/")[-1]
            sl = m.metadata.get("start_line", "?")
            print(f"  {'':30s}  r{i+1:<4d} {m.score:.4f}  {fp}:{sl}")

        print()

        # ── 2. Hybrid score addition (current) ───────────────────────────
        hybrid = await rag.hybrid_search(query, n_results=N_RESULTS, min_score=0.0)
        print(f"  {'HYBRID score-addition (α=0.7)':30s}  rank  score    file:line")
        for i, c in enumerate(hybrid):
            fp = c.file_path.split("/")[-1]
            print(f"  {'':30s}  r{i+1:<4d} {c.score:.4f}  {fp}:{c.start_line}")

        print()

        # ── 3. BM25 + RRF ─────────────────────────────────────────────────
        rrf = await rag.hybrid_search_rrf(query, n_results=N_RESULTS)
        print(f"  {'BM25 + RRF':30s}  rank  score    file:line")
        for i, c in enumerate(rrf):
            fp = c.file_path.split("/")[-1]
            print(f"  {'':30s}  r{i+1:<4d} {c.score:.6f}  {fp}:{c.start_line}")

        print()

    print("Done.")


if __name__ == "__main__":
    asyncio.run(run())
