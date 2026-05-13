"""
Exercise 4: Closing the vocabulary gap with LLM-generated function descriptions.

The vocabulary gap: moveAndRemoveFileFromS3 calls copyObject/deleteObject but
the runtime error is NoSuchKey. That word appears nowhere in the source.
No chunking strategy fixes this — the information simply isn't in the code.

Fix: at index time, generate a 2-3 sentence description per function that
includes what errors it can throw, what it depends on, and what can go wrong.
Embed code + description together. Now "NoSuchKey" is in the chunk's text
and the embedding spans both vocabularies.

Runs three-way comparison:
  line-based (Exercise 2 baseline)
  function-based (Exercise 3 improvement)
  enriched (Exercise 4 — code + generated description)

Usage:
    python scripts/exercise4_enriched_chunks.py
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
from dataclasses import dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

TARGET_FILE    = "/Users/Sidreddy/DevCode/TargetApp/routes/services/image.js"
RELATIVE_PATH  = "routes/services/image.js"
TARGET_FN      = "moveAndRemoveFileFromS3"
TARGET_LINE_CHUNKS = {441, 481}

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
BOLD   = "\033[1m"
GREY   = "\033[90m"


# ---------------------------------------------------------------------------
# Function extractor (same as Exercise 3)
# ---------------------------------------------------------------------------

@dataclass
class FunctionChunk:
    name: str
    file_path: str
    start_line: int
    end_line: int
    content: str
    description: str = ""

    @property
    def chunk_id(self) -> str:
        return hashlib.sha256(f"fn:{self.file_path}:{self.start_line}".encode()).hexdigest()[:16]

    @property
    def enriched_text(self) -> str:
        if self.description:
            return f"{self.content}\n\n// {self.description}"
        return self.content


def extract_functions(content: str, file_path: str) -> list[FunctionChunk]:
    lines = content.splitlines()
    functions: list[FunctionChunk] = []
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
                        start_line=start + 1,
                        end_line=j + 1,
                        content="\n".join(lines[start:j + 1]),
                    ))
                    i = j + 1
                    break
            else:
                i += 1
        else:
            i += 1
    return functions


# ---------------------------------------------------------------------------
# LLM description generation
# ---------------------------------------------------------------------------

DESCRIPTION_PROMPT = """\
You are indexing JavaScript source code for semantic search. \
Write a single sentence (max 40 words) describing this function. \
Include: what it does, what external APIs or services it calls, \
and what errors or exceptions it can throw (use the exact error names \
from those APIs, e.g. NoSuchKey, NotFound, ValidationError). \
Do not include the function name. Output only the sentence, no preamble."""


async def generate_description(client, code: str) -> str:
    """Call Claude Haiku to generate a search-optimised function description."""
    msg = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[{
            "role": "user",
            "content": f"{DESCRIPTION_PROMPT}\n\n```javascript\n{code[:1500]}\n```",
        }],
    )
    return msg.content[0].text.strip()


# ---------------------------------------------------------------------------
# Index helpers
# ---------------------------------------------------------------------------

async def index_fn_collection(rag, functions: list[FunctionChunk], collection, use_enriched: bool) -> int:
    from app.services.vector_store import VectorItem

    texts = [fn.enriched_text if use_enriched else fn.content for fn in functions]
    embeddings = await rag._embed(texts)

    collection.upsert([
        VectorItem(
            id=fn.chunk_id,
            document=fn.enriched_text if use_enriched else fn.content,
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


async def search_collection(rag, collection, query: str, n: int = 10):
    embedding = await rag._embed([query])
    return collection.query(embedding[0], n_results=n)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
    from dotenv import load_dotenv
    load_dotenv()

    import anthropic
    from app.services.rag import RAGService
    from app.services.vector_store import make_collection

    rag = RAGService()
    anthropic_client = anthropic.AsyncAnthropic()

    # Collections
    fn_collection       = make_collection("codebase_fn")        # Exercise 3
    enriched_collection = make_collection("codebase_fn_enriched")  # Exercise 4

    # ── Step 1: extract functions ────────────────────────────────────────
    content   = open(TARGET_FILE).read()
    functions = extract_functions(content, RELATIVE_PATH)
    print(f"{CYAN}Extracted {len(functions)} functions{RESET}\n")

    # ── Step 2: generate descriptions ───────────────────────────────────
    print(f"{CYAN}Generating descriptions with Claude Haiku...{RESET}")
    for fn in functions:
        fn.description = await generate_description(anthropic_client, fn.content)
        marker = " ◀ target" if fn.name == TARGET_FN else ""
        print(f"  {fn.name}{marker}")
        if fn.name == TARGET_FN:
            print(f"    {GREY}→ {fn.description}{RESET}")

    # ── Step 3: index enriched collection ───────────────────────────────
    print(f"\n{CYAN}Indexing enriched collection...{RESET}")
    await index_fn_collection(rag, functions, enriched_collection, use_enriched=True)

    # Make sure fn_collection (plain function chunks) is also indexed
    if fn_collection.count() == 0:
        print(f"{CYAN}Indexing plain function collection...{RESET}")
        await index_fn_collection(rag, functions, fn_collection, use_enriched=False)

    # ── Step 4: three-way comparison ────────────────────────────────────
    print(f"\n{BOLD}Three-way comparison — target: {TARGET_FN}{RESET}\n")
    print(f"{'Query':<43}  {'Line-based':^17}  {'Fn-boundary':^17}  {'Enriched':^17}")
    print(f"{'':43}  {'rank  score':^17}  {'rank  score':^17}  {'rank  score':^17}")
    print("-" * 100)

    for query in QUERIES:
        # Line-based
        line_results = await rag.search(query, n_results=10)
        lr, ls = "–", "–"
        for i, r in enumerate(line_results, 1):
            if r.start_line in TARGET_LINE_CHUNKS:
                lr, ls = str(i), f"{r.score:.4f}"
                break

        # Function-boundary
        fn_results = await search_collection(rag, fn_collection, query)
        fr, fs = "–", "–"
        for i, m in enumerate(fn_results, 1):
            if m.metadata.get("function_name") == TARGET_FN:
                fr, fs = str(i), f"{m.score:.4f}"
                break

        # Enriched
        en_results = await search_collection(rag, enriched_collection, query)
        er, es = "–", "–"
        for i, m in enumerate(en_results, 1):
            if m.metadata.get("function_name") == TARGET_FN:
                er, es = str(i), f"{m.score:.4f}"
                break

        # Colour enriched green if it found the target and fn-boundary didn't (or ranked better)
        fb_num = int(fr) if fr != "–" else 99
        en_num = int(er) if er != "–" else 99
        color = GREEN if en_num < fb_num else (YELLOW if en_num == fb_num else RESET)

        print(
            f"  {query:<41}  "
            f"{'r'+lr+' '+ls:^17}  "
            f"{'r'+fr+' '+fs:^17}  "
            f"{color}{'r'+er+' '+es:^17}{RESET}"
        )

    # ── Step 5: show the enriched chunk ─────────────────────────────────
    target = next(f for f in functions if f.name == TARGET_FN)
    print(f"\n{CYAN}Enriched chunk for {TARGET_FN}:{RESET}")
    print(f"  Code: lines {target.start_line}–{target.end_line}")
    print(f"  Description appended:\n    {GREY}{target.description}{RESET}\n")
    print(f"  Full text sent to embedding API:")
    print(f"  {GREY}{'─'*60}{RESET}")
    for line in target.enriched_text.splitlines():
        print(f"  {GREY}{line}{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
