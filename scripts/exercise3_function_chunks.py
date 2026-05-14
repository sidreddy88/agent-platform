"""
Exercise 3: Function-boundary chunking vs line-based chunking.

Extracts function-boundary chunks from routes/services/image.js, indexes them
into a separate ChromaDB collection ("codebase_fn"), then runs the same 4 queries
from Exercise 2 against both collections side by side.

Usage:
    python scripts/exercise3_function_chunks.py
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TARGET_FILE = "/Users/Sidreddy/DevCode/TargetApp/routes/services/image.js"
RELATIVE_PATH = "routes/services/image.js"

QUERIES = [
    "moveAndRemoveFileFromS3",
    "copy S3 object then delete source",
    "S3 copyObject deleteObject bucket key",
    "S3 NoSuchKey missing key error",
]

RESET  = "\033[0m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
GREY   = "\033[90m"
BOLD   = "\033[1m"


# ---------------------------------------------------------------------------
# Function extractor
# ---------------------------------------------------------------------------

@dataclass
class FunctionChunk:
    name: str
    file_path: str
    start_line: int
    end_line: int
    content: str

    @property
    def chunk_id(self) -> str:
        return hashlib.sha256(f"fn:{self.file_path}:{self.start_line}".encode()).hexdigest()[:16]


def extract_functions(content: str, file_path: str) -> list[FunctionChunk]:
    """
    Extract top-level function declarations from JS/TS content.

    Handles:
      async function name(...)  {
      function name(...)        {

    Uses brace-depth tracking to find the matching closing brace.
    Known limitation: brace characters inside string literals are counted,
    which can cause false positives in files with many inline JSON strings.
    In practice this is rare in the TargetApp codebase.
    """
    lines = content.splitlines()
    functions: list[FunctionChunk] = []

    # Match only top-level function declarations (no leading whitespace)
    func_re = re.compile(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(')

    i = 0
    while i < len(lines):
        m = func_re.match(lines[i])
        if m:
            name = m.group(1)
            start = i
            depth = 0
            found_open = False

            for j in range(i, len(lines)):
                for ch in lines[j]:
                    if ch == '{':
                        depth += 1
                        found_open = True
                    elif ch == '}':
                        depth -= 1
                if found_open and depth <= 0:
                    functions.append(FunctionChunk(
                        name=name,
                        file_path=file_path,
                        start_line=start + 1,   # 1-indexed
                        end_line=j + 1,
                        content="\n".join(lines[start : j + 1]),
                    ))
                    i = j + 1
                    break
            else:
                i += 1
        else:
            i += 1

    return functions


# ---------------------------------------------------------------------------
# Index and compare
# ---------------------------------------------------------------------------

async def index_function_chunks(rag_service, functions: list[FunctionChunk]) -> int:
    from app.services.vector_store import VectorItem

    texts = [f.content for f in functions]
    embeddings = await rag_service._embed(texts)

    rag_service._fn_collection.upsert([
        VectorItem(
            id=fn.chunk_id,
            document=fn.content,
            metadata={
                "chunk_id": fn.chunk_id,
                "file_path": fn.file_path,
                "function_name": fn.name,
                "start_line": fn.start_line,
                "end_line": fn.end_line,
            },
            embedding=emb,
        )
        for fn, emb in zip(functions, embeddings)
    ])
    return len(functions)


async def search_fn_collection(rag_service, query: str, n: int = 5):
    embedding = await rag_service._embed([query])
    return rag_service._fn_collection.query(embedding[0], n_results=n)


async def main():
    from dotenv import load_dotenv
    load_dotenv()

    from app.services.rag import RAGService
    from app.services.vector_store import make_collection

    rag = RAGService()
    # Attach a separate function-chunk collection
    rag._fn_collection = make_collection("codebase_fn")

    # ── Step 1: extract functions ────────────────────────────────────────
    content = open(TARGET_FILE).read()
    functions = extract_functions(content, RELATIVE_PATH)

    print(f"{CYAN}Extracted {len(functions)} functions from {RELATIVE_PATH}{RESET}\n")
    print(f"{'Function':<45} {'Lines':>6}  {'Line range'}")
    print("-" * 70)
    for fn in functions:
        n_lines = fn.end_line - fn.start_line + 1
        print(f"  {fn.name:<43} {n_lines:>6}  {fn.start_line}–{fn.end_line}")

    # ── Step 2: index ────────────────────────────────────────────────────
    print(f"\n{CYAN}Indexing into 'codebase_fn' collection...{RESET}")
    n = await index_function_chunks(rag, functions)
    print(f"  Indexed {n} function chunks\n")

    # ── Step 3: compare queries ──────────────────────────────────────────
    # Line-based: chunks containing moveAndRemoveFileFromS3 (lines 441 and 481)
    TARGET_LINE_CHUNKS = {441, 481}
    TARGET_FN = "moveAndRemoveFileFromS3"

    print(f"{BOLD}Side-by-side comparison — target: moveAndRemoveFileFromS3{RESET}\n")
    print(f"{'Query':<45}  {'Line-based':^22}  {'Function-based':^22}")
    print(f"{'':45}  {'rank  score':^22}  {'rank  score':^22}")
    print("-" * 93)

    for query in QUERIES:
        # Line-based search
        line_results = await rag.search(query, n_results=10)
        line_rank, line_score = "–", "–"
        for i, r in enumerate(line_results, 1):
            if r.start_line in TARGET_LINE_CHUNKS:
                line_rank = str(i)
                line_score = f"{r.score:.4f}"
                break

        # Function-based search
        fn_results = await search_fn_collection(rag, query, n=10)
        fn_rank, fn_score = "–", "–"
        for i, m in enumerate(fn_results, 1):
            if m.metadata.get("function_name") == TARGET_FN:
                fn_rank = str(i)
                fn_score = f"{m.score:.4f}"
                break

        # Colour: green if function-based ranks better
        lb = int(line_rank) if line_rank != "–" else 99
        fb = int(fn_rank) if fn_rank != "–" else 99
        color = GREEN if fb < lb else (YELLOW if fb == lb else RESET)

        print(
            f"  {query:<43}  "
            f"{'rank '+line_rank+' '+line_score:^22}  "
            f"{color}{'rank '+fn_rank+' '+fn_score:^22}{RESET}"
        )

    # ── Step 4: show the winning function chunk ──────────────────────────
    print(f"\n{CYAN}Function chunk for moveAndRemoveFileFromS3:{RESET}")
    fn_chunk = next(f for f in functions if f.name == TARGET_FN)
    print(f"  Lines {fn_chunk.start_line}–{fn_chunk.end_line}  ({fn_chunk.end_line - fn_chunk.start_line + 1} lines)\n")
    for i, line in enumerate(fn_chunk.content.splitlines(), start=fn_chunk.start_line):
        print(f"  {i:4d}  {line}")


if __name__ == "__main__":
    asyncio.run(main())
