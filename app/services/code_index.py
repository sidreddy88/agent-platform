"""
Code index for DiagnosisAgent's search_codebase, in shadow mode.

DiagnosisAgent has never had a code index in production (incident_loop constructs it without one), so its
search_codebase tool isn't offered. This module builds the index without changing diagnosis:

- one vector collection per target repo (code_<owner>_<repo>), so stale or unrelated content in the shared default
  collection can't leak into results;
- built from the same local clone diagnosis reads (LocalRepoService), refreshed in the background: at startup and
  at most once an hour after an incident. Re-indexing only re-embeds files whose content changed;
- shadow retrieval after each diagnosis: search the index with the incident's text and record whether the results
  contained the file diagnosis named. Traced as its own agent span (RAGShadow), so the weekly agentstats report
  measures retrieval quality on real incidents before diagnosis depends on it.

Diagnosis only gets the index when DIAGNOSIS_RAG=on and the index is ready (see IncidentLoop); the default is off.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from app.services.tracing import trace_entry

logger = logging.getLogger(__name__)

REFRESH_EVERY_S = 3600          # after an incident, refresh at most this often
SHADOW_RESULTS = 4              # same as search_codebase
SHADOW_MIN_SCORE = 0.45         # same as search_codebase


def collection_name(owner: str, repo: str) -> str:
    # "codeidx_", not the first build's "code_": that collection was built with per-function LLM descriptions and
    # under the old path-only registry; this one is embedding-only and starts clean.
    return "codeidx_" + re.sub(r"[^a-z0-9]+", "_", f"{owner}_{repo}".lower()).strip("_")


def _norm(path: str | None) -> str:
    return (path or "").strip().lstrip("./").lstrip("/")


class CodeIndex:
    def __init__(self, owner: str, repo: str, local_repo: Any = None, rag: Any = None) -> None:
        self.owner, self.repo = owner, repo
        if local_repo is None:
            from app.services.repo import LocalRepoService
            local_repo = LocalRepoService(owner, repo)
        if rag is None:
            from app.services.rag import RAGService
            # Embedding-only: per-function LLM descriptions cost one call per function (2,338 Haiku calls, $0.48,
            # for the production repo's first build); the with/without-RAG test measures this same configuration.
            rag = RAGService(collection_name=collection_name(owner, repo), function_descriptions=False)
        self.local_repo, self.rag = local_repo, rag
        self.ready = False
        self.last_refresh: float | None = None
        self.last_error: str | None = None
        self.chunks_indexed_last = 0
        self._lock = asyncio.Lock()

    async def refresh(self) -> None:
        """Bring the index up to date with the repo's current code. Never raises."""
        if self._lock.locked():
            return
        async with self._lock:
            try:
                await self.local_repo.ensure_fresh()
                path = str(self.local_repo.local_path)
                self.chunks_indexed_last = await self.rag.index_directory(path, root=path)
                self.ready = self.rag._collection.count() > 0
                self.last_refresh, self.last_error = time.time(), None
                logger.info("[CodeIndex] %s/%s refreshed: %d chunks (re)indexed, %d total", self.owner, self.repo,
                            self.chunks_indexed_last, self.rag._collection.count())
            except Exception as exc:  # observability only: never break the pipeline
                self.last_error = str(exc)[:300]
                logger.warning("[CodeIndex] refresh failed for %s/%s: %s", self.owner, self.repo, exc)

    def refresh_due(self) -> bool:
        return self.last_refresh is None or time.time() - self.last_refresh > REFRESH_EVERY_S

    def status(self) -> dict:
        try:
            total = self.rag._collection.count()
        except Exception:
            total = None
        return {"repo": f"{self.owner}/{self.repo}", "collection": collection_name(self.owner, self.repo),
                "ready": self.ready, "total_chunks": total, "chunks_indexed_last_refresh": self.chunks_indexed_last,
                "last_refresh": self.last_refresh, "last_error": self.last_error}


_SHARED: dict[tuple[str, str], CodeIndex] = {}


def shared(owner: str, repo: str, local_repo: Any = None) -> CodeIndex:
    """One CodeIndex per repo per process. Production runs the Orchestrator, which builds its own IncidentLoop,
    and the module-level incident_loop singleton builds another: both must use (and report) the same index."""
    key = (owner, repo)
    if key not in _SHARED:
        _SHARED[key] = CodeIndex(owner, repo, local_repo=local_repo)
    return _SHARED[key]


class RAGShadow:
    """Shadow retrieval for one diagnosis: what search_codebase would have returned for the incident, and whether it
    contained the file diagnosis named. One trace per call (agent name RAGShadow) for the weekly report."""

    def __init__(self, index: CodeIndex) -> None:
        self.index = index

    @trace_entry(input_of=lambda query, diagnosed_file: {"query_chars": len(query or ""),
                                                          "diagnosed_file": diagnosed_file},
                 output_of=lambda r: r)
    async def check(self, query: str, diagnosed_file: str | None) -> dict:
        chunks = await self.index.rag.hybrid_search(query, n_results=SHADOW_RESULTS, min_score=SHADOW_MIN_SCORE)
        files = list(dict.fromkeys(c.file_path for c in chunks))
        target = _norm(diagnosed_file)
        hit = bool(target) and any(_norm(f) == target or _norm(f).endswith("/" + target) or
                                   target.endswith("/" + _norm(f)) for f in files)
        return {"results": len(chunks), "files": files, "diagnosed_file": diagnosed_file,
                "diagnosed_file_retrieved": hit if target else None}


def shadow_query(incident: Any) -> str:
    """The text diagnosis starts from: the incident's title, error type and description."""
    ev = getattr(incident, "error_event", incident)
    parts = [getattr(ev, "title", ""), getattr(ev, "error_type", ""), getattr(ev, "description", "")]
    return "\n".join(str(p) for p in parts if p)[:2000]
