"""
RAG service — indexes a codebase and supports semantic search over
both code chunks and past incidents.

Embeddings: OpenAI text-embedding-3-small (1536-d).
Vector store: backend-agnostic via app.services.vector_store. The factory
picks ChromaDB locally (sqlite dev) or pgvector against the same RDS
Postgres database in production. RAGService doesn't see the difference.

Usage:
    rag = RAGService()
    await rag.index_directory("./app")
    chunks = await rag.search("how is authentication handled", n_results=5)
    file_chunks = await rag.get_file("app/services/github.py")
"""

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from app.core.config import settings
from app.services.vector_store import VectorItem, make_collection

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = "text-embedding-3-small"
COLLECTION_NAME = "codebase"
CHROMA_PATH = ".chromadb"

CHUNK_LINES = 50        # target lines per chunk
OVERLAP_LINES = 10      # lines shared between adjacent chunks

SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".c": "c",
    ".sh": "bash",
    ".md": "markdown",
}

# Files/dirs to skip
IGNORE_PATTERNS = {
    "__pycache__", ".git", ".venv", "venv", "node_modules",
    ".pytest_cache", "dist", "build", ".mypy_cache",
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CodeChunk:
    chunk_id: str           # stable hash of file_path + start_line
    file_path: str          # relative path from indexed root
    language: str
    start_line: int
    end_line: int
    content: str
    # returned only by search()
    score: float = 0.0


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _chunk_file(file_path: str, content: str, language: str) -> list[CodeChunk]:
    """Split file content into overlapping line-based chunks."""
    lines = content.splitlines()
    chunks: list[CodeChunk] = []
    step = CHUNK_LINES - OVERLAP_LINES
    i = 0

    while i < len(lines):
        start = i
        end = min(i + CHUNK_LINES, len(lines))
        chunk_content = "\n".join(lines[start:end])

        if chunk_content.strip():  # skip blank chunks
            chunk_id = hashlib.sha256(
                f"{file_path}:{start}".encode()
            ).hexdigest()[:16]

            chunks.append(CodeChunk(
                chunk_id=chunk_id,
                file_path=file_path,
                language=language,
                start_line=start + 1,   # 1-indexed for humans
                end_line=end,
                content=chunk_content,
            ))

        i += step

    return chunks


_JS_FUNC_RE = re.compile(r'^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(')

def _chunk_js_by_function(file_path: str, content: str, language: str) -> list[CodeChunk]:
    """
    Extract function-boundary chunks from JS/TS source.

    Each top-level `(async) function name(` declaration becomes one chunk spanning
    its declaration line to its closing brace. Short functions get their own chunk
    with no surrounding noise, which eliminates the dilution problem that fixed
    line-count windows produce.

    Falls back to an empty list for files with no top-level function declarations
    (caller should then fall back to _chunk_file).

    Known limitation: brace characters inside string literals are counted. This is
    rare in the TargetApp backend and doesn't affect correctness in practice.
    """
    lines = content.splitlines()
    chunks: list[CodeChunk] = []

    i = 0
    while i < len(lines):
        m = _JS_FUNC_RE.match(lines[i])
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
                    chunk_content = "\n".join(lines[start:j + 1])
                    if chunk_content.strip():
                        chunk_id = hashlib.sha256(
                            f"fn:{file_path}:{start}".encode()
                        ).hexdigest()[:16]
                        chunks.append(CodeChunk(
                            chunk_id=chunk_id,
                            file_path=file_path,
                            language=language,
                            start_line=start + 1,
                            end_line=j + 1,
                            content=chunk_content,
                        ))
                    i = j + 1
                    break
            else:
                i += 1
        else:
            i += 1

    return chunks


# ---------------------------------------------------------------------------
# RAGService
# ---------------------------------------------------------------------------

class RAGService:
    """Indexes code files and past incidents with OpenAI embeddings.

    The vector store is backend-agnostic via app.services.vector_store.make_collection
    — ChromaDB locally, pgvector in production. Re-indexing the same `chunk_id`
    is an upsert in both backends, so calling index_directory() twice is safe.
    """

    def __init__(
        self,
        chroma_path: str = CHROMA_PATH,
        collection_name: str = COLLECTION_NAME,
        openai_api_key: str | None = None,
    ) -> None:
        api_key = openai_api_key or settings.openai_api_key
        if not api_key:
            raise ValueError("OpenAI API key required (set OPENAI_API_KEY in .env)")

        self._openai = AsyncOpenAI(api_key=api_key)
        self._collection = make_collection(collection_name, chroma_path=chroma_path)
        self._incident_collection = make_collection("incidents", chroma_path=chroma_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def index_directory(self, path: str | None = None, root: str | None = None) -> int:
        """
        Recursively index all supported code files under `path`.

        Args:
            path: Directory to index.
            root: Root used to compute relative file paths stored in metadata.
                  Defaults to `path` itself.

        Returns:
            Number of chunks indexed.
        """
        base = Path(path or settings.codebase_path).resolve()
        rel_root = Path(root).resolve() if root else base

        files = list(self._collect_files(base))
        if not files:
            logger.warning("No supported files found in %s", path)
            return 0

        logger.info("Indexing %d files from %s", len(files), path)

        total = 0
        # Process files concurrently in batches of 10 to avoid overwhelming the API
        batch_size = 10
        for i in range(0, len(files), batch_size):
            batch = files[i : i + batch_size]
            results = await asyncio.gather(
                *[self._index_file(fp, rel_root) for fp in batch],
                return_exceptions=True,
            )
            for fp, result in zip(batch, results):
                if isinstance(result, Exception):
                    logger.error("Failed to index %s: %s", fp, result)
                else:
                    total += result

        logger.info("Indexed %d chunks total", total)
        return total

    async def search(self, query: str, n_results: int = 5, min_score: float = 0.0) -> list[CodeChunk]:
        """Semantic search over indexed chunks. Best first, up to n_results.

        min_score filters out chunks below the threshold before returning.
        Recommended: 0.45 for code search (scores below this are noise).
        Default is 0.0 (no filtering) for backwards compatibility.
        """
        if self._collection.count() == 0:
            return []

        embedding = await self._embed([query])
        matches = self._collection.query(embedding[0], n_results=n_results)

        chunks: list[CodeChunk] = []
        for m in matches:
            if m.score < min_score:
                continue
            meta = m.metadata
            chunks.append(CodeChunk(
                chunk_id=meta.get("chunk_id", m.id),
                file_path=meta.get("file_path", ""),
                language=meta.get("language", ""),
                start_line=int(meta.get("start_line", 0)),
                end_line=int(meta.get("end_line", 0)),
                content=m.document,
                score=m.score,
            ))
        return chunks

    async def hybrid_search(
        self,
        query: str,
        n_results: int = 5,
        alpha: float = 0.7,
        min_score: float = 0.0,
    ) -> list[CodeChunk]:
        """Hybrid lexical+semantic search over indexed code chunks.

        Combines vector cosine similarity (weighted alpha) with a lexical match
        bonus (weighted 1-alpha). Particularly effective for code search because:
        - Vector captures semantic meaning (natural language descriptions of behaviour)
        - Lexical rewards exact token presence (function names, error codes, API names)

        hybrid_score = alpha × vector_score + (1-alpha) × lexical_score
        lexical_score = fraction of query tokens found in the document text
        alpha=0.7 keeps semantic as the primary signal; lexical is a tiebreaker.

        Fetches max(n_results×4, 20) candidates so lexical can rescue low-scoring
        but exact-match chunks, then re-ranks and returns top n_results.

        min_score filters out chunks below the threshold before returning.
        Recommended: 0.45 for code search. Default 0.0 for backwards compatibility.
        """
        if self._collection.count() == 0:
            return []

        embedding = await self._embed([query])
        candidates = self._collection.query(
            embedding[0], n_results=max(n_results * 4, 20)
        )

        query_tokens = set(query.lower().split())
        scored: list[tuple[float, CodeChunk]] = []

        for m in candidates:
            vector_score = m.score
            doc_lower = m.document.lower()
            matched = sum(1 for t in query_tokens if t in doc_lower)
            lexical_score = matched / len(query_tokens) if query_tokens else 0.0
            hybrid = alpha * vector_score + (1 - alpha) * lexical_score

            if hybrid < min_score:
                continue

            meta = m.metadata
            scored.append((hybrid, CodeChunk(
                chunk_id=meta.get("chunk_id", m.id),
                file_path=meta.get("file_path", ""),
                language=meta.get("language", ""),
                start_line=int(meta.get("start_line", 0)),
                end_line=int(meta.get("end_line", 0)),
                content=m.document,
                score=round(hybrid, 4),
            )))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [chunk for _, chunk in scored[:n_results]]

    async def get_file(self, path: str) -> list[CodeChunk]:
        """Return all indexed chunks for a specific file, ordered by start line."""
        matches = self._collection.get_by_filter({"file_path": path})
        chunks = [
            CodeChunk(
                chunk_id=m.metadata.get("chunk_id", m.id),
                file_path=m.metadata.get("file_path", ""),
                language=m.metadata.get("language", ""),
                start_line=int(m.metadata.get("start_line", 0)),
                end_line=int(m.metadata.get("end_line", 0)),
                content=m.document,
            )
            for m in matches
        ]
        return sorted(chunks, key=lambda c: c.start_line)

    def indexed_files(self) -> list[str]:
        """Return a deduplicated list of all indexed file paths."""
        seen: set[str] = set()
        paths: list[str] = []
        for meta in self._collection.all_metadata():
            fp = meta.get("file_path")
            if fp and fp not in seen:
                seen.add(fp)
                paths.append(fp)
        return sorted(paths)

    # ------------------------------------------------------------------
    # Incident embeddings (Layer 3 semantic search)
    # ------------------------------------------------------------------

    async def index_incident(self, incident) -> None:
        """Embed and store a terminal incident for future similarity retrieval."""
        if not incident.diagnosis:
            return
        text = (
            f"{incident.error_event.error_type}: {incident.error_event.description[:300]} | "
            f"Root cause: {incident.diagnosis}"
        )

        try:
            embedding = await self._embed([text])
            self._incident_collection.upsert([VectorItem(
                id=incident.id,
                document=text,
                metadata={
                    "incident_id": incident.id,
                    "status": incident.status.value,
                    "error_type": incident.error_event.error_type or "",
                    "service": incident.error_event.service or "",
                    "pr_url": incident.pr_url or "",
                    "fix_description": (incident.fix_description or "")[:200],
                },
                embedding=embedding[0],
            )])
        except Exception as exc:
            logger.warning("[RAG] Failed to index incident %s: %s", incident.id, exc)

    async def search_incidents(self, query: str, n_results: int = 3, min_score: float = 0.80) -> list[dict]:
        """Semantic search over indexed incidents. Returns matches above min_score, best first."""
        if self._incident_collection.count() == 0:
            return []
        try:
            embedding = await self._embed([query])
            results = self._incident_collection.query(embedding[0], n_results=n_results)
            return [
                {
                    "incident_id": m.metadata.get("incident_id", m.id),
                    "status": m.metadata.get("status", ""),
                    "error_type": m.metadata.get("error_type", ""),
                    "service": m.metadata.get("service", ""),
                    "pr_url": m.metadata.get("pr_url", ""),
                    "fix_description": m.metadata.get("fix_description", ""),
                    "text": m.document,
                    "score": m.score,
                }
                for m in results
                if m.score >= min_score
            ]
        except Exception as exc:
            logger.warning("[RAG] Incident search failed: %s", exc)
            return []

    async def hybrid_search_incidents(
        self,
        query: str,
        n_results: int = 3,
        min_score: float = 0.50,
        alpha: float = 0.7,
    ) -> list[dict]:
        """Hybrid lexical + semantic incident search.

        Combines vector similarity (weighted α) with a lexical match bonus
        (weighted 1-α). Useful when the query contains exact identifiers
        (function names, error codes) that vector retrieval underweights.

        hybrid_score = α × vector_score + (1-α) × lexical_score
        where lexical_score = fraction of query tokens found in the document.
        alpha=0.7 means semantic is primary; lexical is a tiebreaker.
        """
        if self._incident_collection.count() == 0:
            return []
        try:
            # Fetch a wider candidate set at min_score=0.0 so lexical can rescue
            # low-scoring but exact-match documents
            embedding = await self._embed([query])
            candidates = self._incident_collection.query(embedding[0], n_results=max(n_results * 4, 20))

            query_tokens = set(query.lower().split())

            scored = []
            for m in candidates:
                vector_score = m.score
                doc_lower = m.document.lower()
                matched = sum(1 for t in query_tokens if t in doc_lower)
                lexical_score = matched / len(query_tokens) if query_tokens else 0.0
                hybrid = alpha * vector_score + (1 - alpha) * lexical_score
                scored.append((hybrid, vector_score, lexical_score, m))

            scored.sort(key=lambda x: x[0], reverse=True)

            return [
                {
                    "incident_id": m.metadata.get("incident_id", m.id),
                    "status":      m.metadata.get("status", ""),
                    "error_type":  m.metadata.get("error_type", ""),
                    "service":     m.metadata.get("service", ""),
                    "pr_url":      m.metadata.get("pr_url", ""),
                    "fix_description": m.metadata.get("fix_description", ""),
                    "text":        m.document,
                    "score":       round(hybrid, 4),
                    "vector_score":   round(vector_score, 4),
                    "lexical_score":  round(lexical_score, 4),
                }
                for hybrid, vector_score, lexical_score, m in scored
                if hybrid >= min_score
            ][:n_results]
        except Exception as exc:
            logger.warning("[RAG] Hybrid search failed: %s", exc)
            return []

    async def rerank_incidents(
        self,
        query: str,
        n_results: int = 3,
        candidate_pool: int = 20,
    ) -> list[dict]:
        """Two-stage retrieval: vector for recall, cross-encoder for precision.

        Stage 1: fetch `candidate_pool` results from vector search at min_score=0.0
        Stage 2: cross-encoder scores every (query, doc) pair jointly, re-sorts,
                 returns top n_results.

        Cross-encoder scores are logits (not cosine similarities). Positive means
        relevant, negative means not. The spread is much wider than vector scores,
        making the relevance signal cleaner.

        Requires: pip install sentence-transformers
        Model:    cross-encoder/ms-marco-MiniLM-L-6-v2 (~90MB, cached after first use)
        """
        if self._incident_collection.count() == 0:
            return []
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            logger.warning("[RAG] sentence-transformers not installed — falling back to vector search")
            return await self.search_incidents(query, n_results=n_results, min_score=0.0)

        try:
            candidates = await self.search_incidents(query, n_results=candidate_pool, min_score=0.0)
            if not candidates:
                return []

            if not hasattr(self, "_cross_encoder"):
                self._cross_encoder = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            ce = self._cross_encoder
            pairs = [(query, r["text"]) for r in candidates]
            ce_scores = ce.predict(pairs)

            for r, ce_score in zip(candidates, ce_scores):
                r["ce_score"] = round(float(ce_score), 4)
                r["vector_score"] = r.pop("score")   # rename for clarity

            reranked = sorted(candidates, key=lambda x: x["ce_score"], reverse=True)
            return reranked[:n_results]
        except Exception as exc:
            logger.warning("[RAG] Cross-encoder rerank failed: %s", exc)
            return await self.search_incidents(query, n_results=n_results, min_score=0.0)

    def clear(self) -> None:
        """Delete all indexed chunks (wipes the codebase collection)."""
        self._collection.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_files(base: Path):
        """Yield all supported, non-ignored files under base."""
        for fp in base.rglob("*"):
            if not fp.is_file():
                continue
            if any(part in IGNORE_PATTERNS for part in fp.parts):
                continue
            if fp.suffix in SUPPORTED_EXTENSIONS:
                yield fp

    async def _index_file(self, file_path: Path, rel_root: Path) -> int:
        """Read, chunk, embed, and upsert one file. Returns chunk count.

        Uses the doc_chunk_registry to:
        1. Skip files whose content hasn't changed (hash check).
        2. Delete stale chunks from the vector store when a file is updated,
           so old vectors don't accumulate silently.
        """
        try:
            raw = file_path.read_bytes()
        except OSError as exc:
            raise RuntimeError(f"Cannot read {file_path}: {exc}") from exc

        content = raw.decode("utf-8", errors="ignore")
        content_hash = hashlib.sha256(raw).hexdigest()
        language = SUPPORTED_EXTENSIONS[file_path.suffix]
        relative = str(file_path.relative_to(rel_root))

        # ── Hash check: skip unchanged files ─────────────────────────────
        if not self._file_changed(relative, content_hash):
            return 0

        # ── Delete stale chunks from previous indexing run ────────────────
        old_ids = self._get_chunk_ids(relative)
        if old_ids:
            self._collection.delete(old_ids)
            self._mark_superseded(relative)

        # ── Chunk ─────────────────────────────────────────────────────────
        if language in ("javascript", "typescript"):
            chunks = _chunk_js_by_function(relative, content, language)
            if not chunks:
                chunks = _chunk_file(relative, content, language)
        else:
            chunks = _chunk_file(relative, content, language)

        if not chunks:
            return 0

        # ── Embed and upsert ──────────────────────────────────────────────
        texts = [c.content for c in chunks]
        embeddings = await self._embed(texts)

        self._collection.upsert([
            VectorItem(
                id=c.chunk_id,
                document=c.content,
                metadata={
                    "chunk_id": c.chunk_id,
                    "file_path": c.file_path,
                    "language": c.language,
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                },
                embedding=emb,
            )
            for c, emb in zip(chunks, embeddings)
        ])

        # ── Register new chunks ───────────────────────────────────────────
        self._register_chunks(relative, [c.chunk_id for c in chunks], content_hash)

        return len(chunks)

    # ------------------------------------------------------------------
    # Document chunk registry helpers
    # ------------------------------------------------------------------

    def _file_changed(self, doc_id: str, content_hash: str) -> bool:
        """Return True if the file is new or its content hash differs from registry."""
        from sqlalchemy import select
        from app.services.database import engine, tables
        try:
            with engine.connect() as conn:
                row = conn.execute(
                    select(tables.doc_chunk_registry.c.content_hash)
                    .where(tables.doc_chunk_registry.c.doc_id == doc_id)
                    .where(tables.doc_chunk_registry.c.status == "active")
                    .limit(1)
                ).fetchone()
            if row is None:
                return True
            return row.content_hash != content_hash
        except Exception as exc:
            logger.warning("[RAG] Registry hash check failed for %s: %s", doc_id, exc)
            return True  # re-index on uncertainty

    def _get_chunk_ids(self, doc_id: str) -> list[str]:
        """Return all active chunk vector IDs for a doc."""
        from sqlalchemy import select
        from app.services.database import engine, tables
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    select(tables.doc_chunk_registry.c.chunk_vector_id)
                    .where(tables.doc_chunk_registry.c.doc_id == doc_id)
                    .where(tables.doc_chunk_registry.c.status == "active")
                ).fetchall()
            return [r.chunk_vector_id for r in rows]
        except Exception as exc:
            logger.warning("[RAG] Registry chunk lookup failed for %s: %s", doc_id, exc)
            return []

    def _mark_superseded(self, doc_id: str) -> None:
        """Mark all active registry rows for a doc as superseded."""
        from sqlalchemy import update
        from app.services.database import engine, tables
        try:
            with engine.begin() as conn:
                conn.execute(
                    update(tables.doc_chunk_registry)
                    .where(tables.doc_chunk_registry.c.doc_id == doc_id)
                    .where(tables.doc_chunk_registry.c.status == "active")
                    .values(status="superseded")
                )
        except Exception as exc:
            logger.warning("[RAG] Registry supersede failed for %s: %s", doc_id, exc)

    def _register_chunks(self, doc_id: str, chunk_ids: list[str], content_hash: str) -> None:
        """Insert new active registry rows for freshly indexed chunks."""
        from datetime import datetime, timezone
        from app.services.database import engine, tables
        now = datetime.now(timezone.utc).isoformat()
        try:
            with engine.begin() as conn:
                conn.execute(
                    tables.doc_chunk_registry.insert(),
                    [
                        {
                            "doc_id": doc_id,
                            "chunk_vector_id": cid,
                            "content_hash": content_hash,
                            "collection": self._collection._name,
                            "indexed_at": now,
                            "status": "active",
                        }
                        for cid in chunk_ids
                    ],
                )
        except Exception as exc:
            logger.warning("[RAG] Registry insert failed for %s: %s", doc_id, exc)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """Fetch embeddings from OpenAI with exponential backoff on 429 rate limits."""
        for attempt in range(4):
            try:
                response = await self._openai.embeddings.create(
                    model=EMBEDDING_MODEL,
                    input=texts,
                )
                return [item.embedding for item in response.data]
            except Exception as exc:
                msg = str(exc)
                is_rate_limit = "429" in msg or "rate_limit" in msg.lower()
                if is_rate_limit and attempt < 3:
                    wait = 2 ** attempt * 5  # 5s, 10s, 20s
                    logger.warning("[RAG] Rate limit hit, retrying in %ds (attempt %d/4)", wait, attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                raise RuntimeError(f"Embedding request failed: {exc}") from exc
        raise RuntimeError("Embedding request failed after 4 attempts")
