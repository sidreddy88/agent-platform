"""
One-shot script: index TargetApp codebase into the RAG vector store.

JS/TS files are indexed using function-boundary chunking — one chunk per top-level
function. This eliminates the dilution problem where short functions get mixed with
unrelated code in a fixed 50-line window. Python/other files fall back to line-based.

Usage:
    python scripts/index_target_app.py                          # index
    python scripts/index_target_app.py --query "classifyFields json parse"
    python scripts/index_target_app.py --search-only --query "NoSuchKey S3"
    python scripts/index_target_app.py --search-only --hybrid --query "NoSuchKey"

The index is persisted to .chromadb/ (ChromaDB sqlite). Re-running is safe — upserts.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TARGET_APP_PATH = "/Users/Sidreddy/DevCode/TargetApp"

RESET  = "\033[0m"
GREEN  = "\033[32m"
CYAN   = "\033[36m"
GREY   = "\033[90m"
YELLOW = "\033[33m"


async def index(path: str) -> None:
    from dotenv import load_dotenv
    load_dotenv()
    from app.services.rag import RAGService

    rag = RAGService()
    print(f"{CYAN}Indexing {path} (JS/TS: function-boundary chunks) ...{RESET}")
    n = await rag.index_directory(path)
    print(f"{GREEN}Done — {n} chunks indexed.{RESET}")
    print(f"  Total chunks in collection: {rag._collection.count()}")


async def search(query: str, n: int = 5, hybrid: bool = False) -> None:
    from dotenv import load_dotenv
    load_dotenv()
    from app.services.rag import RAGService

    rag = RAGService()
    count = rag._collection.count()
    if count == 0:
        print("Collection empty — run without --search-only first to index.")
        return

    mode = "hybrid" if hybrid else "vector"
    print(f"{CYAN}[{mode}] Searching {count} chunks for: '{query}'{RESET}\n")

    if hybrid:
        results = await rag.hybrid_search(query, n_results=n)
    else:
        results = await rag.search(query, n_results=n)

    for i, r in enumerate(results, 1):
        print(f"  {i}. [{r.score:.4f}]  {r.file_path}:{r.start_line}-{r.end_line}")
        print(f"     {GREY}{r.content[:120].replace(chr(10), ' ')}{RESET}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", help="Search query to test")
    parser.add_argument("--n", type=int, default=5, help="Number of results")
    parser.add_argument("--search-only", action="store_true", help="Skip indexing")
    parser.add_argument("--hybrid", action="store_true", help="Use hybrid lexical+semantic search")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if not args.search_only:
        asyncio.run(index(TARGET_APP_PATH))

    if args.query:
        asyncio.run(search(args.query, args.n, hybrid=args.hybrid))
