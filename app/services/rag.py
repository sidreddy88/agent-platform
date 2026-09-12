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

import tiktoken
from openai import AsyncOpenAI

from app.core.config import settings
from app.services.llm_gateway import llm_gateway
from app.services.tracing import _get_client as _lf_client
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

# OpenAI's text-embedding-3-small hard limit is 8192 tokens per input. 8000
# leaves headroom for the enriched-text description appended after this cap
# is applied. cl100k_base is the real tokenizer for this embedding model —
# a chars-per-token guess (~4:1 for English/code) silently fails on dense or
# non-ASCII content, which is exactly the failure this cap exists to catch.
MAX_EMBED_TOKENS = 8000
_EMBED_ENCODING = tiktoken.get_encoding("cl100k_base")


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    tokens = _EMBED_ENCODING.encode(text)
    if len(tokens) <= max_tokens:
        return text
    return _EMBED_ENCODING.decode(tokens[:max_tokens])

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
    # set only for function/method chunks (_chunk_js_by_ast)
    function_name: str | None = None
    kind: str | None = None            # "function" | "arrow" | "method"
    description: str = ""              # LLM-generated, see _generate_function_description
    # returned only by search()
    score: float = 0.0

    @property
    def enriched_text(self) -> str:
        """What actually gets embedded when a description is present — code
        plus a trailing comment, so the embedding carries both the code's own
        vocabulary (API calls, identifiers) and the description's (error
        names, plain-language behavior) the source never states explicitly."""
        if self.description:
            return f"{self.content}\n\n// {self.description}"
        return self.content


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


def _chunk_js_by_ast(file_path: str, content: str, raw: bytes, language: str) -> list[CodeChunk]:
    """
    Extract function-boundary chunks from JS/TS source using the real tree-sitter
    parser already built for the call graph (app.services.code_graph.parser) —
    not a regex.

    One chunk per top-level function/arrow-function/method declaration, each
    spanning its exact byte range. Short functions get their own chunk with no
    surrounding noise, eliminating the dilution problem fixed line-count windows
    produce, and — unlike a `function name(` regex — this also catches arrow
    functions (`const foo = () => {}`) and class methods, which a regex-only
    extractor silently misses. On a 4248-function survey of a real backend, a
    regex matching only `function name(` found ~8% of what tree-sitter finds.

    Falls back to an empty list for files with no top-level definitions or a
    parse error (caller then falls back to _chunk_file).
    """
    from app.services.code_graph.parser import extract_function_definitions, parse_source

    suffix = Path(file_path).suffix
    try:
        tree = parse_source(raw, suffix)
        fn_defs = extract_function_definitions(tree, content)
    except Exception as exc:
        logger.debug("[RAG] tree-sitter parse failed for %s, falling back: %s", file_path, exc)
        return []

    chunks: list[CodeChunk] = []
    for fn in fn_defs:
        chunk_content = raw[fn.start_byte:fn.end_byte].decode("utf-8", errors="ignore")
        if not chunk_content.strip():
            continue
        chunk_id = hashlib.sha256(f"fn:{file_path}:{fn.start_line}".encode()).hexdigest()[:16]
        chunks.append(CodeChunk(
            chunk_id=chunk_id,
            file_path=file_path,
            language=language,
            start_line=fn.start_line,
            end_line=fn.end_line,
            content=chunk_content,
            function_name=fn.name,
            kind=fn.kind,
        ))
    return chunks


_MD_HEADER_RE = re.compile(r'^#{1,6}\s+.+')

def _chunk_markdown_by_headers(file_path: str, content: str, language: str) -> list[CodeChunk]:
    """
    Element-based chunking for Markdown: one chunk per header section, so a
    query like "how do I install this?" lands on the Installation section
    directly instead of a fixed-size window spanning several unrelated ones.

    A section runs from one header line up to (but not including) the next
    header line, regardless of heading level — matches how READMEs are
    actually read: each header names one self-contained topic.

    Falls back to an empty list for files with no headers at all (caller then
    falls back to _chunk_file — e.g. plain-prose docs with no `#` structure).
    """
    lines = content.splitlines()
    sections: list[tuple[int, int]] = []  # (start_idx, end_idx) exclusive, 0-indexed
    start = None
    for i, line in enumerate(lines):
        if _MD_HEADER_RE.match(line):
            if start is not None:
                sections.append((start, i))
            start = i
    if start is not None:
        sections.append((start, len(lines)))

    chunks: list[CodeChunk] = []
    for start_idx, end_idx in sections:
        chunk_content = "\n".join(lines[start_idx:end_idx]).strip()
        if not chunk_content:
            continue
        chunk_id = hashlib.sha256(f"md:{file_path}:{start_idx}".encode()).hexdigest()[:16]
        chunks.append(CodeChunk(
            chunk_id=chunk_id,
            file_path=file_path,
            language=language,
            start_line=start_idx + 1,
            end_line=end_idx,
            content=chunk_content,
        ))
    return chunks


DESCRIPTION_PROMPT = (
    "You are indexing JavaScript source code for semantic search. "
    "Write a single sentence (max 40 words) describing this function. "
    "Include: what it does, what external APIs or services it calls, "
    "and what errors or exceptions it can throw (use the exact error names "
    "from those APIs, e.g. NoSuchKey, NotFound, ValidationError). "
    "Do not include the function name. Output only the sentence, no preamble."
)


# Caps concurrent Haiku calls across all files in an index_directory() batch —
# index_directory already runs 10 files concurrently, and a single file can
# have dozens of functions, so without this a big directory pass could fire
# hundreds of simultaneous description calls and trip a rate limit.
_DESCRIPTION_SEMAPHORE = asyncio.Semaphore(10)


async def _describe_with_limit(llm_service, code: str) -> str:
    async with _DESCRIPTION_SEMAPHORE:
        return await _generate_function_description(llm_service, code)


async def _generate_function_description(llm_service, code: str) -> str:
    """Call Haiku (routed through llm_gateway's 'code_description' task, so
    it's cost-tracked the same as every other agent call) to generate a
    description spanning both the code's own vocabulary and the vocabulary
    of whatever it can fail with — the vocabulary gap a bare code embedding
    can't close on its own. Returns "" on any failure; the caller embeds the
    plain code chunk in that case, same as a chunk with no description."""
    try:
        text = await llm_service.complete(
            messages=[{
                "role": "user",
                "content": f"{DESCRIPTION_PROMPT}\n\n```javascript\n{code[:1500]}\n```",
            }],
        )
        return text.strip()
    except Exception as exc:
        logger.debug("[RAG] Description generation failed: %s", exc)
        return ""


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
        self._collection_name = collection_name
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

        lf = _lf_client()
        start = time.perf_counter()

        async def _run() -> list[CodeChunk]:
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
                    function_name=meta.get("function_name") or None,
                    kind=meta.get("kind") or None,
                    score=m.score,
                ))
            return chunks

        if lf is None:
            return await _run()

        with lf.start_as_current_observation(
            name="rag.search",
            as_type="span",
            input={"query": query, "n_results": n_results, "min_score": min_score},
        ) as obs:
            chunks = await _run()
            obs.update(
                output={"num_results": len(chunks), "top_score": chunks[0].score if chunks else None},
                metadata={
                    "collection": self._collection_name,
                    "duration_ms": int((time.perf_counter() - start) * 1000),
                    "results": [
                        {"rank": i, "score": c.score, "file_path": c.file_path,
                         "chunk_id": c.chunk_id, "start_line": c.start_line}
                        for i, c in enumerate(chunks)
                    ],
                },
            )
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

        lf = _lf_client()
        start = time.perf_counter()

        async def _run() -> list[CodeChunk]:
            embedding = await self._embed([query])
            candidates = self._collection.query(
                embedding[0], n_results=max(n_results * 4, 20)
            )
            query_tokens = set(query.lower().split())
            scored: list[tuple[float, float, float, CodeChunk]] = []
            for m in candidates:
                vector_score = m.score
                doc_lower = m.document.lower()
                matched = sum(1 for t in query_tokens if t in doc_lower)
                lexical_score = matched / len(query_tokens) if query_tokens else 0.0
                hybrid = alpha * vector_score + (1 - alpha) * lexical_score
                if hybrid < min_score:
                    continue
                meta = m.metadata
                scored.append((hybrid, vector_score, lexical_score, CodeChunk(
                    chunk_id=meta.get("chunk_id", m.id),
                    file_path=meta.get("file_path", ""),
                    language=meta.get("language", ""),
                    start_line=int(meta.get("start_line", 0)),
                    end_line=int(meta.get("end_line", 0)),
                    content=m.document,
                    function_name=meta.get("function_name") or None,
                    kind=meta.get("kind") or None,
                    score=round(hybrid, 4),
                )))
            scored.sort(key=lambda x: x[0], reverse=True)
            return scored[:n_results]

        if lf is None:
            return [c for _, _, _, c in await _run()]

        with lf.start_as_current_observation(
            name="rag.hybrid_search",
            as_type="span",
            input={"query": query, "n_results": n_results, "alpha": alpha, "min_score": min_score},
        ) as obs:
            scored = await _run()
            chunks = [c for _, _, _, c in scored]
            obs.update(
                output={"num_results": len(chunks), "top_score": chunks[0].score if chunks else None},
                metadata={
                    "collection": self._collection_name,
                    "duration_ms": int((time.perf_counter() - start) * 1000),
                    "results": [
                        {"rank": i, "score": c.score, "vector_score": round(vs, 4),
                         "lexical_score": round(ls, 4), "file_path": c.file_path,
                         "chunk_id": c.chunk_id, "start_line": c.start_line}
                        for i, (_, vs, ls, c) in enumerate(scored)
                    ],
                },
            )
        return chunks

    async def hybrid_search_rrf(
        self,
        query: str,
        n_results: int = 5,
        vector_pool: int = 20,
        bm25_pool: int = 20,
        k: int = 60,
        min_score: float = 0.0,
    ) -> list[CodeChunk]:
        """Hybrid search using BM25 (full corpus) + vector, fused with RRF.

        Unlike hybrid_search() which adds scores (requires comparable scales),
        RRF combines ranked lists by position — safe when BM25 produces unbounded
        scores incomparable to cosine similarity.

        Stage 1: vector search → top vector_pool results ranked by cosine similarity.
        Stage 2: BM25 over the FULL corpus → top bm25_pool results ranked by BM25 score.
        Stage 3: RRF fusion of the two ranked lists — scores ignored, positions only.

        Running BM25 over the full corpus is what allows it to surface chunks the
        vector search missed — the key property that makes RRF > reranking the
        vector candidate pool.
        """
        if self._collection.count() == 0:
            return []

        try:
            from rank_bm25 import BM25Okapi
        except ImportError:
            return await self.hybrid_search(query, n_results=n_results, min_score=min_score)

        # Stage 1: vector search
        embedding = await self._embed([query])
        vector_candidates = self._collection.query(embedding[0], n_results=max(vector_pool, 20))
        vector_ranking = [m.id for m in vector_candidates]  # ordered by cosine score

        # Stage 2: BM25 over the full corpus
        all_items = self._collection.all_items()
        if not all_items:
            return []

        id_to_item = {m.id: m for m in all_items}
        corpus_ids = [m.id for m in all_items]
        tokenized_corpus = [m.document.lower().split() for m in all_items]

        bm25 = BM25Okapi(tokenized_corpus)
        query_tokens = query.lower().split()
        bm25_scores = bm25.get_scores(query_tokens)
        bm25_ranking = [corpus_ids[i]
                        for i in sorted(range(len(corpus_ids)),
                                        key=lambda i: bm25_scores[i], reverse=True)
                        ][:bm25_pool]

        # Stage 3: RRF — combine both ranked lists
        rrf: dict[str, float] = {}
        for rank, doc_id in enumerate(vector_ranking):
            rrf[doc_id] = rrf.get(doc_id, 0.0) + 1.0 / (k + rank + 1)
        for rank, doc_id in enumerate(bm25_ranking):
            rrf[doc_id] = rrf.get(doc_id, 0.0) + 1.0 / (k + rank + 1)

        fused = sorted(rrf.items(), key=lambda x: x[1], reverse=True)

        results = []
        for doc_id, rrf_score in fused[:n_results]:
            m = id_to_item.get(doc_id)
            if m is None:
                continue
            meta = m.metadata
            results.append(CodeChunk(
                chunk_id=meta.get("chunk_id", doc_id),
                file_path=meta.get("file_path", ""),
                language=meta.get("language", ""),
                start_line=int(meta.get("start_line", 0)),
                end_line=int(meta.get("end_line", 0)),
                content=m.document,
                function_name=meta.get("function_name") or None,
                kind=meta.get("kind") or None,
                score=round(rrf_score, 6),
            ))
        return results

    async def rerank_functions(
        self,
        query: str,
        n_results: int = 3,
        candidate_pool: int = 20,
    ) -> list[CodeChunk]:
        """Two-stage retrieval for code: vector for recall, cross-encoder for precision.

        Mirrors rerank_incidents()'s exact pattern — same cached CrossEncoder
        instance and model — applied to the codebase collection.

        Built for the case hybrid_search() alone can't resolve: several
        functions in the same file tie on lexical score (e.g. every S3
        function mentions "s3", "key", "error"), and vector score alone
        compresses them into a band too narrow to separate confidently. A
        cross-encoder reads the query and each candidate's full text jointly
        and can tell a function whose description explicitly discusses the
        error being searched for apart from one that just shares its
        vocabulary incidentally.

        Requires: pip install sentence-transformers
        Model:    cross-encoder/ms-marco-MiniLM-L-6-v2 (~90MB, cached after
                  first use — shared with rerank_incidents() via the same
                  self._cross_encoder instance).
        """
        if self._collection.count() == 0:
            return []
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            logger.warning("[RAG] sentence-transformers not installed — falling back to vector search")
            return await self.search(query, n_results=n_results)

        try:
            candidates = await self.search(query, n_results=candidate_pool, min_score=0.0)
            if not candidates:
                return []

            if not hasattr(self, "_cross_encoder"):
                self._cross_encoder = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
            ce = self._cross_encoder
            pairs = [(query, c.content) for c in candidates]
            ce_scores = ce.predict(pairs)

            for c, ce_score in zip(candidates, ce_scores):
                c.score = round(float(ce_score), 4)

            reranked = sorted(candidates, key=lambda c: c.score, reverse=True)
            return reranked[:n_results]
        except Exception as exc:
            logger.warning("[RAG] Cross-encoder rerank failed: %s", exc)
            return await self.search(query, n_results=n_results)

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
                function_name=m.metadata.get("function_name") or None,
                kind=m.metadata.get("kind") or None,
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

        lf = _lf_client()
        start = time.perf_counter()

        async def _run() -> list[dict]:
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

        try:
            if lf is None:
                return await _run()

            with lf.start_as_current_observation(
                name="rag.search_incidents",
                as_type="span",
                input={"query": query, "n_results": n_results, "min_score": min_score},
            ) as obs:
                hits = await _run()
                obs.update(
                    output={"num_results": len(hits), "top_score": hits[0]["score"] if hits else None},
                    metadata={
                        "collection": "incidents",
                        "duration_ms": int((time.perf_counter() - start) * 1000),
                        "results": [
                            {"rank": i, "score": h["score"], "incident_id": h["incident_id"],
                             "error_type": h["error_type"]}
                            for i, h in enumerate(hits)
                        ],
                    },
                )
            return hits
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

        lf = _lf_client()
        start = time.perf_counter()

        async def _run() -> list[dict]:
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

        try:
            if lf is None:
                return await _run()

            with lf.start_as_current_observation(
                name="rag.hybrid_search_incidents",
                as_type="span",
                input={"query": query, "n_results": n_results, "alpha": alpha, "min_score": min_score},
            ) as obs:
                hits = await _run()
                obs.update(
                    output={"num_results": len(hits), "top_score": hits[0]["score"] if hits else None},
                    metadata={
                        "collection": "incidents",
                        "duration_ms": int((time.perf_counter() - start) * 1000),
                        "results": [
                            {"rank": i, "score": h["score"], "vector_score": h["vector_score"],
                             "lexical_score": h["lexical_score"], "incident_id": h["incident_id"],
                             "error_type": h["error_type"]}
                            for i, h in enumerate(hits)
                        ],
                    },
                )
            return hits
        except Exception as exc:
            logger.warning("[RAG] Hybrid search failed: %s", exc)
            return []

    async def rerank_incidents(
        self,
        query: str,
        n_results: int = 3,
        candidate_pool: int = 20,
        min_score: float = 0.80,
    ) -> list[dict]:
        """Two-stage retrieval: vector for recall, cross-encoder for precision.

        Stage 1: fetch `candidate_pool` results from vector search at min_score.
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
            return await self.search_incidents(query, n_results=n_results, min_score=min_score)

        try:
            candidates = await self.search_incidents(query, n_results=candidate_pool, min_score=min_score)
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
            return await self.search_incidents(query, n_results=n_results, min_score=min_score)

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

        # ── Chunk — routed by file type, not one strategy for everything ────
        if language in ("javascript", "typescript"):
            chunks = _chunk_js_by_ast(relative, content, raw, language)
            if not chunks:
                chunks = _chunk_file(relative, content, language)
        elif language == "markdown":
            chunks = _chunk_markdown_by_headers(relative, content, language)
            if not chunks:
                chunks = _chunk_file(relative, content, language)
        else:
            chunks = _chunk_file(relative, content, language)

        if not chunks:
            return 0

        # ── Defensive cap — some chunks (huge generated files, or files with
        # unusually dense/non-ASCII content where chars-per-token is far from
        # the ~4:1 English/code rule of thumb) exceed OpenAI's 8192-token
        # embedding limit. _embed() below sends the whole file's chunks as one
        # batch call, so one oversized chunk fails the batch and silently
        # drops every other (perfectly fine) chunk in that file with it.
        # Truncate by real token count (tiktoken), not a character guess.
        for c in chunks:
            c.content = _truncate_to_tokens(c.content, MAX_EMBED_TOKENS)

        # ── Enrich function/method chunks with an LLM-generated description ─
        # Only chunks that came from _chunk_js_by_ast have function_name set;
        # markdown sections and line-based chunks have no single function to
        # describe, so they're embedded as plain code/text either way.
        if settings.rag_enable_function_descriptions:
            fn_chunks = [c for c in chunks if c.function_name]
            if fn_chunks:
                llm_service = llm_gateway.get_llm_service_for("code_description")
                descriptions = await asyncio.gather(*[
                    _describe_with_limit(llm_service, c.content) for c in fn_chunks
                ])
                for c, desc in zip(fn_chunks, descriptions):
                    c.description = desc

        # ── Embed and upsert ──────────────────────────────────────────────
        texts = [c.enriched_text for c in chunks]
        embeddings = await self._embed(texts)

        self._collection.upsert([
            VectorItem(
                id=c.chunk_id,
                document=c.enriched_text,
                metadata={
                    "chunk_id": c.chunk_id,
                    "file_path": c.file_path,
                    "language": c.language,
                    "start_line": c.start_line,
                    "end_line": c.end_line,
                    "function_name": c.function_name or "",
                    "kind": c.kind or "",
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
