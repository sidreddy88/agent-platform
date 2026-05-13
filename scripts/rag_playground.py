"""
Interactive RAG debugger — deep local exploration of retrieval behaviour.

Run from repo root:

    python scripts/rag_playground.py

Uses the same `RAGService` the agents use. Talks directly to the local
ChromaDB (under `.chromadb/`) when DATABASE_URL points at SQLite, or to
pgvector if you've pointed DATABASE_URL at a Postgres instance.

Useful for:
  - Iterating on queries without spinning up uvicorn
  - Inspecting embedding vectors themselves, not just scores
  - Seeing the distribution of scores across the whole corpus for a query
  - Comparing how rephrasing a query shifts the top-K
  - Trying cross-encoder re-ranking on top of vector retrieval
  - Examining indexed text directly

Commands (typed at the `> ` prompt):

  # Retrieval
    q <query>           Top-10 incident search with scores
    c <query>           Top-10 codebase search with scores
    topk <k> <query>    Variable top-K (default of `q`/`c` is 10)
    dist <query>        Score distribution across THE ENTIRE corpus
                        (text histogram — shows hit, near-miss, cold-start)
    nearest <text>      Top-3 docs closest to the given text — useful for
                        checking "is this content in the corpus?"

  # Embedding inspection
    embed <text>        Show dim, L2 norm, first 8 values of the vector
    norm <text>         Just the dim + norm (faster)
    cmp <a> | <b>       Cosine similarity between two arbitrary strings
                        (uses the same embedder; no corpus needed)
    pair <a> | <b>      Run both queries; show Jaccard overlap of their
                        top-10 result sets. Reveals query sensitivity.

  # Corpus inspection
    stats               Counts + indexed-text length distribution per corpus
    list                Dump every indexed_text in the incident corpus
    chunks <file>       Show how RAGService would chunk a single file
                        (50-line chunks with 10-line overlap)

  # Index management
    index <path>        Recursively index a directory into the codebase corpus

  # Advanced
    rerank <query>      Cross-encoder re-rank of top-10 (requires
                        `pip install sentence-transformers` — prints a
                        helpful hint if missing)

    quit / ^D / ^C      Exit

A bare query (no command prefix) is treated as `q <query>`.
"""
from __future__ import annotations

import asyncio
import math
import os
import sys
from typing import Optional

# Allow running from repo root without installing the package
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.rag import RAGService, CHUNK_LINES, OVERLAP_LINES  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    return dot / (mag_a * mag_b) if mag_a and mag_b else 0.0


def _histogram(scores: list[float], width: int = 40) -> None:
    """Text-mode histogram of score distribution.

    Bins from 0.0 to 1.0 in steps of 0.05. The width is the longest bar.
    """
    bins = [0] * 20  # [0.00,0.05), [0.05,0.10), ..., [0.95,1.00]
    for s in scores:
        idx = min(int(max(s, 0.0) * 20), 19)
        bins[idx] += 1
    if not bins or max(bins) == 0:
        print("  (no scores to plot)")
        return
    scale = width / max(bins)
    for i, count in enumerate(bins):
        lo, hi = i * 0.05, (i + 1) * 0.05
        bar = "█" * int(count * scale)
        print(f"  {lo:.2f}–{hi:.2f}  {bar} ({count})")


_RERANKER: Optional[object] = None


def _get_reranker():
    """Lazy-load the cross-encoder; prints a helpful hint if missing."""
    global _RERANKER
    if _RERANKER is not None:
        return _RERANKER
    try:
        from sentence_transformers import CrossEncoder  # type: ignore

        _RERANKER = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
        return _RERANKER
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Commands — Retrieval
# ---------------------------------------------------------------------------


async def _cmd_query(rag: RAGService, text: str, *, k: int = 10, threshold: float = 0.80) -> None:
    if rag._incident_collection.count() == 0:
        print("  (incident corpus is empty — no documents indexed locally)")
        return
    results = await rag.search_incidents(text, n_results=k, min_score=0.0)
    if not results:
        print("  (no results — likely an embedding call failed; check OPENAI_API_KEY)")
        return
    for r in results:
        marker = "✓" if r["score"] >= threshold else "·"
        body = r["text"].replace("\n", " ")[:120]
        print(f"  {marker} {r['score']:.3f}  {r['error_type'] or '?':<28} {body}")


async def _cmd_codebase(rag: RAGService, text: str, *, k: int = 10) -> None:
    if rag._collection.count() == 0:
        print("  (codebase corpus is empty — run `index <path>` first)")
        return
    chunks = await rag.search(text, n_results=k)
    for c in chunks:
        print(f"  {c.score:.3f}  {c.file_path}:{c.start_line}-{c.end_line}")


async def _cmd_topk(rag: RAGService, rest: str) -> None:
    parts = rest.strip().split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        print("  usage: topk <k> <query>")
        return
    k = max(1, min(int(parts[0]), 100))
    await _cmd_query(rag, parts[1], k=k)


async def _cmd_distribution(rag: RAGService, text: str) -> None:
    """Score histogram across the WHOLE corpus, not just top-K.

    Reveals what 'hit' vs 'near-miss' vs 'cold-start' looks like for the
    actual corpus. A spiky distribution at high scores = corpus is dense
    with relevant docs. A wide low distribution = cold start territory.
    """
    n = rag._incident_collection.count()
    if n == 0:
        print("  (corpus empty)")
        return
    results = await rag.search_incidents(text, n_results=n, min_score=0.0)
    if not results:
        print("  (search returned nothing — embedding may have failed)")
        return
    scores = [r["score"] for r in results]
    print(f"  Score distribution across {len(scores)} docs (query: {text[:60]})")
    _histogram(scores)
    top, median, mean = max(scores), sorted(scores)[len(scores)//2], sum(scores)/len(scores)
    print(f"  top={top:.3f}  median={median:.3f}  mean={mean:.3f}")


async def _cmd_nearest(rag: RAGService, text: str) -> None:
    """Same as `q` but framed for 'is this content indexed?' usage.

    If the corpus contains text very similar to the query, top-1 should
    be > 0.85 (often near 1.0 if it's literally indexed). Useful for
    sanity-checking 'I just indexed X, is it there?'
    """
    if rag._incident_collection.count() == 0:
        print("  (corpus empty)")
        return
    results = await rag.search_incidents(text, n_results=3, min_score=0.0)
    if not results:
        print("  (no results)")
        return
    for r in results:
        body = r["text"].replace("\n", " ")[:100]
        verdict = "near-identical" if r["score"] >= 0.95 \
            else "very close" if r["score"] >= 0.85 \
            else "related" if r["score"] >= 0.60 \
            else "unrelated"
        print(f"  {r['score']:.3f} [{verdict:<15}] {body}")


# ---------------------------------------------------------------------------
# Commands — Embedding inspection
# ---------------------------------------------------------------------------


async def _cmd_embed(rag: RAGService, text: str) -> None:
    """Show the actual vector — first 8 dims + norm + magnitude.

    Useful for building intuition: 'what does the model actually output?'
    The values are small floats; the vector is L2-normalised by OpenAI;
    individual dims rarely tell you anything by themselves.
    """
    embs = await rag._embed([text])
    vec = embs[0]
    norm = math.sqrt(sum(x * x for x in vec))
    head = ", ".join(f"{v:+.4f}" for v in vec[:8])
    print(f"  dim    = {len(vec)}")
    print(f"  ||v||₂ = {norm:.6f}")
    print(f"  first 8 dims: [{head}, ...]")
    print(f"  text:  {text[:120]}")


async def _cmd_norm(rag: RAGService, text: str) -> None:
    embs = await rag._embed([text])
    vec = embs[0]
    norm = math.sqrt(sum(x * x for x in vec))
    print(f"  dim   = {len(vec)}")
    print(f"  ||v||₂ = {norm:.6f}   (text-embedding-3-small is L2-normalised, so this should be ~1.0)")


async def _cmd_compare(rag: RAGService, rest: str) -> None:
    if "|" not in rest:
        print("  usage: cmp <text1> | <text2>")
        return
    a, b = [s.strip() for s in rest.split("|", 1)]
    if not a or not b:
        print("  both sides must be non-empty")
        return
    embs = await rag._embed([a, b])
    score = _cosine(embs[0], embs[1])
    verdict = (
        "near-identical" if score >= 0.95
        else "same topic" if score >= 0.70
        else "related" if score >= 0.45
        else "weakly related" if score >= 0.25
        else "unrelated"
    )
    print(f"  cosine = {score:.4f}  [{verdict}]")
    print(f"    a: {a[:80]}")
    print(f"    b: {b[:80]}")


async def _cmd_pair(rag: RAGService, rest: str) -> None:
    """Run two queries; show Jaccard overlap of their top-10 result sets.

    Sensitivity probe: if 'jwt expired' and 'token has expired' return
    very different top-10s, your retrieval is brittle to phrasing.
    """
    if "|" not in rest:
        print("  usage: pair <query1> | <query2>")
        return
    a, b = [s.strip() for s in rest.split("|", 1)]
    if not a or not b:
        print("  both sides must be non-empty")
        return
    results_a = await rag.search_incidents(a, n_results=10, min_score=0.0)
    results_b = await rag.search_incidents(b, n_results=10, min_score=0.0)
    ids_a = {r["incident_id"] for r in results_a}
    ids_b = {r["incident_id"] for r in results_b}
    intersection = ids_a & ids_b
    union = ids_a | ids_b
    jaccard = len(intersection) / len(union) if union else 0.0
    print(f"  Jaccard(top-10) = {jaccard:.2f}  ({len(intersection)} shared / {len(union)} unique)")
    print(f"    a: {a[:60]}  → top scores: {[round(r['score'],3) for r in results_a[:5]]}")
    print(f"    b: {b[:60]}  → top scores: {[round(r['score'],3) for r in results_b[:5]]}")


# ---------------------------------------------------------------------------
# Commands — Corpus inspection
# ---------------------------------------------------------------------------


async def _cmd_stats(rag: RAGService) -> None:
    inc_n = rag._incident_collection.count()
    code_n = rag._collection.count()
    print(f"  Incident corpus: {inc_n} docs")
    if inc_n:
        items = list(rag._incident_collection.all_items())
        lengths = [len(it.document) for it in items]
        lengths.sort()
        med = lengths[len(lengths)//2]
        p90 = lengths[int(len(lengths)*0.9)]
        print(f"    text length:  min={min(lengths)}  median={med}  p90={p90}  max={max(lengths)}")
        # Service / error_type breakdown
        by_type: dict[str, int] = {}
        for it in items:
            et = it.metadata.get("error_type", "?")
            by_type[et] = by_type.get(et, 0) + 1
        print(f"    top error types:")
        for et, count in sorted(by_type.items(), key=lambda x: -x[1])[:8]:
            print(f"      {count:4d}  {et}")
    print(f"  Codebase corpus: {code_n} chunks")
    if code_n:
        # Codebase doesn't expose all_items() easily without all_metadata
        try:
            metas = list(rag._collection.all_metadata())
            by_file: dict[str, int] = {}
            for m in metas:
                fp = m.get("file_path", "?")
                by_file[fp] = by_file.get(fp, 0) + 1
            print(f"    files indexed: {len(by_file)}")
            print(f"    top 5 files by chunk count:")
            for fp, count in sorted(by_file.items(), key=lambda x: -x[1])[:5]:
                print(f"      {count:4d}  {fp}")
        except Exception:
            pass


async def _cmd_list(rag: RAGService) -> None:
    if rag._incident_collection.count() == 0:
        print("  (corpus empty)")
        return
    for item in rag._incident_collection.all_items():
        meta = item.metadata
        body = item.document.replace("\n", " ")[:140]
        print(f"  {meta.get('incident_id', '?')[:8]}  {meta.get('error_type','?'):<28} {body}")


def _cmd_chunks(file_path: str) -> None:
    """Show how RAGService.chunk_file would split a single file.

    Replicates the chunking logic locally so you can see exactly where
    chunk boundaries fall before indexing. Useful for diagnosing
    'why is my function split across two chunks?'
    """
    fp = os.path.expanduser(file_path.strip())
    if not os.path.isfile(fp):
        print(f"  not a file: {fp}")
        return
    try:
        text = open(fp, encoding="utf-8", errors="replace").read()
    except Exception as exc:
        print(f"  could not read {fp}: {exc}")
        return
    lines = text.splitlines()
    n_lines = len(lines)
    print(f"  {fp}: {n_lines} lines")
    print(f"  CHUNK_LINES={CHUNK_LINES}  OVERLAP_LINES={OVERLAP_LINES}")
    start = 0
    chunk_n = 0
    while start < n_lines:
        end = min(start + CHUNK_LINES, n_lines)
        # First non-empty line as preview
        preview = next((ln.strip() for ln in lines[start:end] if ln.strip()), "")
        print(f"    chunk {chunk_n}: lines {start+1}-{end} ({end-start} lines) | {preview[:80]}")
        chunk_n += 1
        if end >= n_lines:
            break
        start = end - OVERLAP_LINES


async def _cmd_index(rag: RAGService, path: str) -> None:
    path = path.strip()
    if not path:
        print("  usage: index <path>")
        return
    expanded = os.path.expanduser(path)
    if not os.path.isdir(expanded):
        print(f"  not a directory: {expanded}")
        return
    print(f"  indexing {expanded} … (may take a minute for a real codebase)")
    before = rag._collection.count()
    n = await rag.index_directory(expanded)
    after = rag._collection.count()
    print(f"  indexed {n} chunks  (codebase corpus: {before} → {after})")


# ---------------------------------------------------------------------------
# Commands — Advanced (cross-encoder re-rank)
# ---------------------------------------------------------------------------


async def _cmd_rerank(rag: RAGService, text: str) -> None:
    """Re-rank vector top-10 with a cross-encoder.

    The cross-encoder scores each (query, doc) pair directly with a small
    transformer, rather than via cosine similarity of separately-encoded
    vectors. Vector retrieval is fast but coarse; cross-encoder is slow
    but precise. The standard pattern is: vector retrieval for top-K,
    cross-encoder for re-ranking that K to top-3 or top-5.

    Requires `pip install sentence-transformers`.
    """
    reranker = _get_reranker()
    if reranker is None:
        print("  Cross-encoder not installed. To enable:")
        print("    pip install sentence-transformers")
        print("  Then restart the playground.")
        return
    if rag._incident_collection.count() == 0:
        print("  (corpus empty)")
        return
    print("  running vector retrieval (top-10)…")
    results = await rag.search_incidents(text, n_results=10, min_score=0.0)
    if not results:
        print("  (no vector results)")
        return

    print("  running cross-encoder re-rank…")
    pairs = [(text, r["text"][:512]) for r in results]
    ce_scores = reranker.predict(pairs).tolist()

    # Show side-by-side: original rank vs new rank
    enriched = [
        {**r, "ce_score": ce, "orig_rank": i + 1}
        for i, (r, ce) in enumerate(zip(results, ce_scores))
    ]
    reranked = sorted(enriched, key=lambda x: -x["ce_score"])

    print(f"\n  {'new':>3} {'orig':>5} {'vec':>7} {'CE':>8}  body")
    for new_rank, r in enumerate(reranked, 1):
        body = r["text"].replace("\n", " ")[:80]
        moved = r["orig_rank"] - new_rank
        arrow = f" ({'+' if moved > 0 else ''}{moved})" if moved else ""
        print(f"  {new_rank:>3} {r['orig_rank']:>5} {r['score']:>7.3f} {r['ce_score']:>8.3f}  {body}{arrow}")


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------


async def main() -> None:
    rag = RAGService()
    incident_n = rag._incident_collection.count()
    codebase_n = rag._collection.count()
    print(f"RAG playground")
    print(f"  incident corpus: {incident_n} docs")
    print(f"  codebase corpus: {codebase_n} chunks")
    print("  type 'help' for commands, 'quit' to exit")

    while True:
        try:
            line = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("quit", "exit"):
            break
        if line == "help":
            # Print everything between "Commands" and the next "A bare query"
            if "Commands" in __doc__:
                _, after = __doc__.split("Commands", 1)
                print(after.split("A bare query")[0].rstrip())
            continue
        try:
            if line == "list":
                await _cmd_list(rag)
            elif line == "stats":
                await _cmd_stats(rag)
            elif line.startswith("cmp "):
                await _cmd_compare(rag, line[4:])
            elif line.startswith("pair "):
                await _cmd_pair(rag, line[5:])
            elif line.startswith("topk "):
                await _cmd_topk(rag, line[5:])
            elif line.startswith("dist "):
                await _cmd_distribution(rag, line[5:])
            elif line.startswith("nearest "):
                await _cmd_nearest(rag, line[8:])
            elif line.startswith("q "):
                await _cmd_query(rag, line[2:])
            elif line.startswith("c "):
                await _cmd_codebase(rag, line[2:])
            elif line.startswith("embed "):
                await _cmd_embed(rag, line[6:])
            elif line.startswith("norm "):
                await _cmd_norm(rag, line[5:])
            elif line.startswith("index "):
                await _cmd_index(rag, line[6:])
            elif line.startswith("chunks "):
                _cmd_chunks(line[7:])
            elif line.startswith("rerank "):
                await _cmd_rerank(rag, line[7:])
            else:
                await _cmd_query(rag, line)
        except Exception as exc:
            print(f"  error: {exc}")


if __name__ == "__main__":
    asyncio.run(main())
