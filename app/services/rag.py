"""
RAG service — indexes a codebase into ChromaDB and supports semantic search.

Embeddings: OpenAI text-embedding-3-small
Vector store: ChromaDB (persistent, local)

Usage:
    rag = RAGService()
    await rag.index_directory("./app")
    chunks = await rag.search("how is authentication handled", n_results=5)
    file_chunks = await rag.get_file("app/services/github.py")
"""

import asyncio
import hashlib
import logging
from pathlib import Path
from dataclasses import dataclass

import chromadb
from openai import AsyncOpenAI

from app.core.config import settings

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
    """
    Indexes code files into ChromaDB with OpenAI embeddings.

    The ChromaDB collection persists on disk at CHROMA_PATH so re-indexing
    is incremental — already-indexed chunks (same chunk_id) are upserted,
    not duplicated.
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
        self._chroma = chromadb.PersistentClient(path=chroma_path)
        self._collection = self._chroma.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )
        self._incident_collection = self._chroma.get_or_create_collection(
            name="incidents",
            metadata={"hnsw:space": "cosine"},
        )

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
        """
        Semantic search over indexed chunks.

        Returns up to `n_results` chunks ordered by relevance (best first).
        """
        if self._collection.count() == 0:
            return []

        embedding = await self._embed([query])
        results = self._collection.query(
            query_embeddings=embedding,
            n_results=min(n_results, self._collection.count()),
            include=["documents", "metadatas", "distances"],
        )

        chunks: list[CodeChunk] = []
        for doc, meta, dist in zip(
            results["documents"][0],
            results["metadatas"][0],
            results["distances"][0],
        ):
            chunks.append(CodeChunk(
                chunk_id=meta["chunk_id"],
                file_path=meta["file_path"],
                language=meta["language"],
                start_line=meta["start_line"],
                end_line=meta["end_line"],
                content=doc,
                score=round(1 - dist, 4),  # cosine similarity (higher = better)
            ))

        return chunks

    async def get_file(self, path: str) -> list[CodeChunk]:
        """
        Return all indexed chunks for a specific file, ordered by start line.

        `path` should match the relative path stored during indexing.
        """
        results = self._collection.get(
            where={"file_path": path},
            include=["documents", "metadatas"],
        )

        chunks = [
            CodeChunk(
                chunk_id=meta["chunk_id"],
                file_path=meta["file_path"],
                language=meta["language"],
                start_line=meta["start_line"],
                end_line=meta["end_line"],
                content=doc,
            )
            for doc, meta in zip(results["documents"], results["metadatas"])
        ]

        return sorted(chunks, key=lambda c: c.start_line)

    def indexed_files(self) -> list[str]:
        """Return a deduplicated list of all indexed file paths."""
        if self._collection.count() == 0:
            return []
        results = self._collection.get(include=["metadatas"])
        seen: set[str] = set()
        paths: list[str] = []
        for meta in results["metadatas"]:
            fp = meta["file_path"]
            if fp not in seen:
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
            f"{incident.error_event.title} | {incident.error_event.error_type} | "
            f"{incident.error_event.service} | {incident.error_event.description[:200]} | "
            f"Root cause: {incident.diagnosis}"
        )
        if incident.fix_description:
            text += f" | Fix: {incident.fix_description}"
        text += f" | Outcome: {incident.status.value}"

        try:
            embedding = await self._embed([text])
            self._incident_collection.upsert(
                ids=[incident.id],
                documents=[text],
                embeddings=embedding,
                metadatas=[{
                    "incident_id": incident.id,
                    "status": incident.status.value,
                    "error_type": incident.error_event.error_type or "",
                    "service": incident.error_event.service or "",
                    "pr_url": incident.pr_url or "",
                }],
            )
        except Exception as exc:
            logger.warning("[RAG] Failed to index incident %s: %s", incident.id, exc)

    async def search_incidents(self, query: str, n_results: int = 3, min_score: float = 0.80) -> list[dict]:
        """Semantic search over indexed incidents. Returns matches above min_score, best first."""
        count = self._incident_collection.count()
        if count == 0:
            return []
        try:
            embedding = await self._embed([query])
            results = self._incident_collection.query(
                query_embeddings=embedding,
                n_results=min(n_results, count),
                include=["documents", "metadatas", "distances"],
            )
            matches = []
            for doc, meta, dist in zip(
                results["documents"][0],
                results["metadatas"][0],
                results["distances"][0],
            ):
                score = round(1 - dist, 4)
                if score >= min_score:
                    matches.append({
                        "incident_id": meta["incident_id"],
                        "status": meta["status"],
                        "error_type": meta["error_type"],
                        "service": meta["service"],
                        "pr_url": meta["pr_url"],
                        "text": doc,
                        "score": score,
                    })
            return matches
        except Exception as exc:
            logger.warning("[RAG] Incident search failed: %s", exc)
            return []

    def clear(self) -> None:
        """Delete all indexed chunks (wipes the collection)."""
        self._chroma.delete_collection(COLLECTION_NAME)
        self._collection = self._chroma.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

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

        self._collection.upsert(
            ids=[c.chunk_id for c in chunks],
            documents=texts,
            embeddings=embeddings,
            metadatas=[
                {
                    "chunk_id": c.chunk_id,
                    "file_path": c.file_path,
                    "language": c.language,
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                }
                for c in chunks
            ],
        )

        return len(chunks)

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        """Fetch embeddings from OpenAI, handling rate limits with one retry."""
        try:
            response = await self._openai.embeddings.create(
                model=EMBEDDING_MODEL,
                input=texts,
            )
        except Exception as exc:
            # Surface a clear error rather than a cryptic OpenAI SDK exception
            raise RuntimeError(f"Embedding request failed: {exc}") from exc

        # API returns embeddings in the same order as input
        return [item.embedding for item in response.data]
