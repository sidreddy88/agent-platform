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

    async def search(self, query: str, n_results: int = 5) -> list[CodeChunk]:
        """Semantic search over indexed chunks. Best first, up to n_results."""
        if self._collection.count() == 0:
            return []

        embedding = await self._embed([query])
        matches = self._collection.query(embedding[0], n_results=n_results)

        chunks: list[CodeChunk] = []
        for m in matches:
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
        """Read, chunk, embed, and upsert one file. Returns chunk count."""
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            raise RuntimeError(f"Cannot read {file_path}: {exc}") from exc

        language = SUPPORTED_EXTENSIONS[file_path.suffix]
        relative = str(file_path.relative_to(rel_root))
        chunks = _chunk_file(relative, content, language)

        if not chunks:
            return 0

        # Embed all chunks in one API call (up to 2048 inputs per request)
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

        return len(chunks)

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
