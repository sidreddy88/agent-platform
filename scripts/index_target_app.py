"""
One-shot script: index TargetApp codebase into the RAG vector store.

Usage:
    python scripts/index_target_app.py
    python scripts/index_target_app.py --query "classifyFields json parse"

The index is persisted to .chromadb/ (ChromaDB sqlite). Re-running is safe — upserts.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TARGET_APP_PATH = "/Users/Sidreddy/DevCode/TargetApp"

RESET = "\033[0m"
GREEN = "\033[32m"
CYAN  = "\033[36m"
GREY  = "\033[90m"


async def index(path: str) -> None:
    from app.services.rag import RAGService
    from dotenv import load_dotenv
    load_dotenv()

    rag = RAGService()
    print(f"{CYAN}Indexing {path} ...{RESET}")
    n = await rag.index_directory(path)
    print(f"{GREEN}Done — {n} chunks indexed.{RESET}")
    print(f"  Total chunks in collection: {rag._collection.count()}")


async def search(query: str, n: int = 5) -> None:
    from app.services.rag import RAGService
    from dotenv import load_dotenv
    load_dotenv()

    rag = RAGService()
    count = rag._collection.count()
    if count == 0:
        print("Collection empty — run without --query first to index.")
        return

    print(f"{CYAN}Searching {count} chunks for: '{query}'{RESET}\n")
    results = await rag.search(query, n_results=n)
    for i, r in enumerate(results, 1):
        print(f"  {i}. [{r.score:.4f}]  {r.file_path}:{r.start_line}-{r.end_line}")
        print(f"     {GREY}{r.content[:120].replace(chr(10), ' ')}{RESET}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", help="Search query to test after indexing")
    parser.add_argument("--n", type=int, default=5, help="Number of results")
    parser.add_argument("--search-only", action="store_true", help="Skip indexing, just search")
    args = parser.parse_args()

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    if not args.search_only:
        asyncio.run(index(TARGET_APP_PATH))

    if args.query:
        asyncio.run(search(args.query, args.n))
