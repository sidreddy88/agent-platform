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


@router.post("/code-graph/index")
async def index_code_graph(background_tasks: BackgroundTasks):
    """
    Trigger a full call graph index of the target codebase.

    If CODEBASE_PATH is set and exists locally, uses it. Otherwise clones
    fix_target_repo from GitHub via GITHUB_TOKEN (shallow clone, deleted after).
    Runs in the background — returns immediately. Edges persist in Postgres
    and are loaded into memory on the next server restart.
    """
    import logging
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path

    from app.core.config import settings
    from app.services.code_graph.graph import CodeGraph
    from app.services.code_graph.store import clear_all_edges, persist_edges

    async def _index():
        log = logging.getLogger(__name__)
        cloned_dir = None
        try:
            local = settings.codebase_path
            if local and Path(local).is_dir():
                codebase_path = local
            else:
                repo = settings.fix_target_repo
                if not repo:
                    log.error("[CodeGraph] No codebase_path and no fix_target_repo configured")
                    return
                token = settings.github_token
                if not token:
                    log.error("[CodeGraph] GITHUB_TOKEN not set — cannot clone %s", repo)
                    return
                cloned_dir = tempfile.mkdtemp(prefix="code_graph_clone_")
                url = f"https://x-access-token:{token}@github.com/{repo}.git"

                # Plain `git clone` checks out whatever GitHub's *default*
                # branch is configured to be, which isn't necessarily where
                # active development happens (DiagnosisAgent's grounding hit
                # this exact issue in PR #129 -- this repo's active branch
                # is 'staging', not 'main'). Clone the real default branch
                # explicitly rather than assuming; a stale/wrong branch here
                # silently produces a code graph missing whole files, with
                # find_callers() just returning empty results instead of an
                # error.
                from app.services.github import GitHubService
                owner, name = repo.split("/", 1)
                default_branch = await GitHubService().get_default_branch(owner, name)
                log.info("[CodeGraph] Cloning %s (branch: %s) ...", repo, default_branch)
                result = subprocess.run(
                    ["git", "clone", "--depth=1", "--branch", default_branch, url, cloned_dir],
                    capture_output=True, text=True,
                )
                if result.returncode != 0:
                    log.error("[CodeGraph] Clone failed: %s", result.stderr)
                    return
                codebase_path = cloned_dir

            log.info("[CodeGraph] Indexing %s ...", codebase_path)
            clear_all_edges()
            graph = CodeGraph.build_from_directory(codebase_path)
            persisted = persist_edges(graph._edges)
            stats = graph.stats()
            log.info(
                "[CodeGraph] Done — %d edges, %d callers, %d callees",
                persisted, stats["unique_callers"], stats["unique_callees"],
            )
        finally:
            if cloned_dir:
                shutil.rmtree(cloned_dir, ignore_errors=True)

    background_tasks.add_task(_index)
    return {"status": "indexing_started", "note": "Edges persist to Postgres. Restart server to load into memory."}


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

    items = rag._incident_collection.all_items()
    docs = [
        {
            "incident_id": item.metadata.get("incident_id", item.id),
            "status": item.metadata.get("status", ""),
            "error_type": item.metadata.get("error_type", ""),
            "service": item.metadata.get("service", ""),
            "pr_url": item.metadata.get("pr_url") or None,
            "indexed_text": item.document,
            "indexed_text_length": len(item.document),
        }
        for item in items
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

    file_chunks: dict[str, int] = {}
    for meta in rag._collection.all_metadata():
        fp = meta.get("file_path", "")
        if fp:
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
