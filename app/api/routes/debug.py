"""
RAG debug endpoints — inspect retrieval quality without changing production behaviour.

GET /debug/rag?query=<text>&n=5&threshold=0.80
    Run a query against the incident collection. Shows ALL results up to n,
    with a pass/fail marker against threshold. Reveals near-misses clearly.

GET /debug/rag/corpus
    List every document currently indexed in the incident collection.
    Shows incident_id, status, service, and the exact text that was embedded.
"""
from fastapi import APIRouter, BackgroundTasks, HTTPException, Query

router = APIRouter(prefix="/debug", tags=["debug"])


@router.post("/rag/index")
async def index_codebase(background_tasks: BackgroundTasks):
    """
    Trigger codebase indexing into the RAG vector store.

    Reads CODEBASE_PATH from settings and indexes all supported code files.
    Runs in the background — returns immediately. Check corpus size via
    GET /debug/rag/corpus once complete.
    """
    from app.core.config import settings

    if not settings.codebase_path:
        raise HTTPException(status_code=400, detail="CODEBASE_PATH not configured in .env")

    try:
        from app.services.rag import RAGService
        rag = RAGService()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"RAG unavailable: {exc}")

    async def _index():
        count = await rag.index_directory(settings.codebase_path)
        import logging
        logging.getLogger(__name__).info("[RAG] Indexed %d chunks from %s", count, settings.codebase_path)

    background_tasks.add_task(_index)
    return {"status": "indexing_started", "path": settings.codebase_path}


@router.get("/rag")
async def debug_rag(
    query: str,
    n: int = Query(default=5, ge=1, le=20),
    threshold: float = Query(default=0.80, ge=0.0, le=1.0),
):
    """
    Query the incident RAG collection and show every result with score,
    pass/fail against threshold, and the exact text that was embedded.
    """
    try:
        from app.services.rag import RAGService
        rag = RAGService()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"RAG unavailable: {exc}")

    corpus_size = rag._incident_collection.count()
    # Use min_score=0.0 to surface all results including near-misses
    results = await rag.search_incidents(query, n_results=n, min_score=0.0)

    return {
        "query": query,
        "threshold": threshold,
        "corpus_size": corpus_size,
        "results_returned": len(results),
        "results": [
            {
                "rank": i + 1,
                "incident_id": r["incident_id"],
                "score": r["score"],
                "passes_threshold": r["score"] >= threshold,
                "status": r["status"],
                "error_type": r["error_type"],
                "service": r["service"],
                "pr_url": r["pr_url"] or None,
                "indexed_text": r["text"],
            }
            for i, r in enumerate(results)
        ],
        "diagnosis": _diagnose(results, threshold),
    }


@router.get("/rag/corpus")
async def debug_rag_corpus():
    """List every document in the incident collection with its indexed text."""
    try:
        from app.services.rag import RAGService
        rag = RAGService()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"RAG unavailable: {exc}")

    count = rag._incident_collection.count()
    if count == 0:
        return {"count": 0, "documents": []}

    raw = rag._incident_collection.get(include=["documents", "metadatas"])
    docs = [
        {
            "incident_id": meta["incident_id"],
            "status": meta["status"],
            "error_type": meta["error_type"],
            "service": meta["service"],
            "pr_url": meta["pr_url"] or None,
            "indexed_text": doc,
            "indexed_text_length": len(doc),
        }
        for doc, meta in zip(raw["documents"], raw["metadatas"])
    ]
    return {
        "count": count,
        "avg_text_length": round(sum(d["indexed_text_length"] for d in docs) / count),
        "documents": docs,
    }


@router.get("/rag/corpus/codebase")
async def debug_codebase_corpus():
    """List stats for the indexed codebase collection (file paths + chunk counts)."""
    try:
        from app.services.rag import RAGService
        rag = RAGService()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"RAG unavailable: {exc}")

    count = rag._collection.count()
    if count == 0:
        return {"count": 0, "files": []}

    raw = rag._collection.get(include=["metadatas"])
    file_chunks: dict[str, int] = {}
    for meta in raw["metadatas"]:
        fp = meta["file_path"]
        file_chunks[fp] = file_chunks.get(fp, 0) + 1

    return {
        "total_chunks": count,
        "total_files": len(file_chunks),
        "files": [
            {"file_path": fp, "chunks": n}
            for fp, n in sorted(file_chunks.items())
        ],
    }


def _diagnose(results: list[dict], threshold: float) -> str:
    """Return a short human-readable hint about what the results suggest."""
    if not results:
        return "No results — corpus is empty or query embedding failed."
    top = results[0]["score"]
    passing = [r for r in results if r["score"] >= threshold]
    near_miss = [r for r in results if threshold - 0.10 <= r["score"] < threshold]

    if top >= threshold:
        return f"Good — top result passes threshold ({top:.3f} ≥ {threshold}). {len(passing)} result(s) total."
    if near_miss:
        return (
            f"Near-miss — top score {top:.3f} is just below threshold {threshold}. "
            f"Consider lowering threshold or enriching indexed text."
        )
    if top >= 0.60:
        return (
            f"Weak match — top score {top:.3f}. Either the corpus lacks a similar incident "
            f"or the indexed text doesn't capture enough semantic signal."
        )
    return f"No semantic match — top score {top:.3f}. This is a genuine cold start."
