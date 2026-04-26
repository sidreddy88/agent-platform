"""
Tests for RAGService.

Two modes:
  - Unit tests (default): no OpenAI or ChromaDB calls — fully mocked.
  - Live test (opt-in):   requires OPENAI_API_KEY and indexes the real codebase.

Run unit tests:
    pytest tests/test_rag.py -v

Run live test (indexes /Users/Sidreddy/VoyageCode/SerpApiTestTool):
    pytest tests/test_rag.py -m live -s
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.rag import (
    CHUNK_LINES,
    OVERLAP_LINES,
    RAGService,
    _chunk_file,
)

CODEBASE_PATH = "/Users/Sidreddy/VoyageCode/SerpApiTestTool"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_CODE = "\n".join(f"line {i}" for i in range(1, 121))  # 120 lines


def make_rag(collection_count: int = 10) -> RAGService:
    """Return a RAGService with all external dependencies mocked."""
    with patch("app.services.rag.AsyncOpenAI"), \
         patch("app.services.rag.chromadb.PersistentClient") as mock_chroma:

        mock_col = MagicMock()
        mock_col.count.return_value = collection_count
        mock_chroma.return_value.get_or_create_collection.return_value = mock_col

        rag = RAGService(openai_api_key="sk-test")
        rag._collection = mock_col
        rag._openai = MagicMock()
        rag._openai.embeddings = MagicMock()
        rag._openai.embeddings.create = AsyncMock(
            return_value=MagicMock(
                data=[MagicMock(embedding=[0.1] * 1536)]
            )
        )
        return rag


# ---------------------------------------------------------------------------
# Unit tests — chunking
# ---------------------------------------------------------------------------

class TestChunkFile:
    def test_single_chunk_for_short_file(self):
        content = "\n".join(f"line {i}" for i in range(1, 10))
        chunks = _chunk_file("app/foo.py", content, "python")
        assert len(chunks) == 1
        assert chunks[0].start_line == 1
        assert chunks[0].language == "python"
        assert chunks[0].file_path == "app/foo.py"

    def test_multiple_chunks_for_long_file(self):
        chunks = _chunk_file("app/big.py", SAMPLE_CODE, "python")
        # 120 lines, step=40 → chunks starting at 0, 40, 80 → 3 chunks
        assert len(chunks) == 3

    def test_overlap_between_chunks(self):
        chunks = _chunk_file("app/big.py", SAMPLE_CODE, "python")
        # Second chunk should start OVERLAP_LINES before first chunk ends
        step = CHUNK_LINES - OVERLAP_LINES
        assert chunks[1].start_line == step + 1

    def test_chunk_ids_are_unique(self):
        chunks = _chunk_file("app/big.py", SAMPLE_CODE, "python")
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids))

    def test_same_file_same_ids(self):
        """Chunk IDs must be stable across calls (deterministic hashing)."""
        chunks_a = _chunk_file("app/foo.py", SAMPLE_CODE, "python")
        chunks_b = _chunk_file("app/foo.py", SAMPLE_CODE, "python")
        assert [c.chunk_id for c in chunks_a] == [c.chunk_id for c in chunks_b]

    def test_blank_file_returns_no_chunks(self):
        assert _chunk_file("app/empty.py", "   \n\n  ", "python") == []

    def test_line_numbers_are_1_indexed(self):
        chunks = _chunk_file("app/foo.py", SAMPLE_CODE, "python")
        assert chunks[0].start_line == 1


# ---------------------------------------------------------------------------
# Unit tests — RAGService.index_directory
# ---------------------------------------------------------------------------

class TestIndexDirectory:
    @pytest.mark.asyncio
    async def test_indexes_supported_files(self, tmp_path):
        (tmp_path / "main.py").write_text("def hello(): pass\n" * 5)
        (tmp_path / "app.js").write_text("const x = 1;\n" * 5)
        (tmp_path / "README.md").write_text("# Readme\n")
        (tmp_path / "data.csv").write_text("a,b,c\n")  # unsupported — should be skipped

        rag = make_rag()
        rag._collection.upsert = MagicMock()
        rag._openai.embeddings.create = AsyncMock(
            return_value=MagicMock(data=[MagicMock(embedding=[0.1] * 1536)])
        )

        count = await rag.index_directory(str(tmp_path))
        assert count > 0
        assert rag._collection.upsert.called

    @pytest.mark.asyncio
    async def test_skips_ignored_directories(self, tmp_path):
        node_modules = tmp_path / "node_modules"
        node_modules.mkdir()
        (node_modules / "lib.js").write_text("const x = 1;")
        (tmp_path / "index.js").write_text("const y = 2;")

        rag = make_rag()
        rag._collection.upsert = MagicMock()

        await rag.index_directory(str(tmp_path))
        # Only index.js should have been upserted, not node_modules/lib.js
        for call in rag._collection.upsert.call_args_list:
            metadatas = call.kwargs.get("metadatas") or call.args[3]
            for meta in metadatas:
                assert "node_modules" not in meta["file_path"]

    @pytest.mark.asyncio
    async def test_uses_default_codebase_path_when_none_given(self):
        rag = make_rag()
        collected = []

        async def fake_index_file(fp, rel_root):
            collected.append(str(fp))
            return 0

        rag._index_file = fake_index_file

        with patch("app.services.rag.settings") as mock_settings:
            mock_settings.codebase_path = "/fake/path"
            with patch.object(rag, "_collect_files", return_value=[]):
                await rag.index_directory()  # no path argument

    @pytest.mark.asyncio
    async def test_empty_directory_returns_zero(self, tmp_path):
        rag = make_rag()
        count = await rag.index_directory(str(tmp_path))
        assert count == 0


# ---------------------------------------------------------------------------
# Unit tests — RAGService.search
# ---------------------------------------------------------------------------

class TestSearch:
    @pytest.mark.asyncio
    async def test_returns_code_chunks(self):
        rag = make_rag(collection_count=5)
        rag._collection.query = MagicMock(return_value={
            "documents": [["def foo(): pass"]],
            "metadatas": [[{
                "chunk_id": "abc123",
                "file_path": "app/foo.py",
                "language": "python",
                "start_line": 1,
                "end_line": 10,
            }]],
            "distances": [[0.1]],
        })

        results = await rag.search("foo function")
        assert len(results) == 1
        assert results[0].file_path == "app/foo.py"
        assert results[0].score == pytest.approx(0.9, abs=0.001)

    @pytest.mark.asyncio
    async def test_empty_collection_returns_empty_list(self):
        rag = make_rag(collection_count=0)
        results = await rag.search("anything")
        assert results == []

    @pytest.mark.asyncio
    async def test_respects_n_results(self):
        rag = make_rag(collection_count=20)
        rag._collection.query = MagicMock(return_value={
            "documents": [[]],
            "metadatas": [[]],
            "distances": [[]],
        })

        await rag.search("query", n_results=3)
        call_kwargs = rag._collection.query.call_args.kwargs
        assert call_kwargs["n_results"] == 3


# ---------------------------------------------------------------------------
# Unit tests — RAGService.get_file
# ---------------------------------------------------------------------------

class TestGetFile:
    @pytest.mark.asyncio
    async def test_returns_chunks_sorted_by_line(self):
        rag = make_rag()
        rag._collection.get = MagicMock(return_value={
            "documents": ["chunk B content", "chunk A content"],
            "metadatas": [
                {"chunk_id": "b", "file_path": "app/foo.py", "language": "python",
                 "start_line": 51, "end_line": 100},
                {"chunk_id": "a", "file_path": "app/foo.py", "language": "python",
                 "start_line": 1, "end_line": 50},
            ],
        })

        chunks = await rag.get_file("app/foo.py")
        assert chunks[0].start_line == 1
        assert chunks[1].start_line == 51

    @pytest.mark.asyncio
    async def test_queries_by_file_path(self):
        rag = make_rag()
        rag._collection.get = MagicMock(return_value={"documents": [], "metadatas": []})

        await rag.get_file("app/services/github.py")
        call_kwargs = rag._collection.get.call_args.kwargs
        assert call_kwargs["where"] == {"file_path": "app/services/github.py"}


# ---------------------------------------------------------------------------
# Unit tests — RAGService.clear
# ---------------------------------------------------------------------------

class TestClear:
    def test_clears_and_recreates_collection(self):
        rag = make_rag()
        rag._chroma = MagicMock()
        new_col = MagicMock()
        rag._chroma.get_or_create_collection.return_value = new_col

        rag.clear()

        rag._chroma.delete_collection.assert_called_once()
        assert rag._collection is new_col


# ---------------------------------------------------------------------------
# Live integration test — indexes the real SerpApiTestTool repo
# ---------------------------------------------------------------------------

@pytest.mark.live
@pytest.mark.asyncio
async def test_live_index_and_search():
    """
    Indexes /Users/Sidreddy/VoyageCode/SerpApiTestTool and runs a search.

    Requires OPENAI_API_KEY in .env.

    Run with:
        pytest tests/test_rag.py -m live -s
    """
    import os
    if not os.environ.get("OPENAI_API_KEY") and not __import__("app.core.config", fromlist=["settings"]).settings.openai_api_key:
        pytest.skip("OPENAI_API_KEY not set")

    if not Path(CODEBASE_PATH).exists():
        pytest.skip(f"Codebase path not found: {CODEBASE_PATH}")

    rag = RAGService(chroma_path=".chromadb_test")

    print(f"\nIndexing {CODEBASE_PATH}...")
    count = await rag.index_directory(CODEBASE_PATH)
    print(f"Indexed {count} chunks")
    assert count > 0

    print("\nSearching for 'route handler'...")
    results = await rag.search("route handler", n_results=3)
    assert len(results) > 0
    for r in results:
        print(f"  [{r.score:.3f}] {r.file_path}:{r.start_line}-{r.end_line}")
        print(f"  {r.content[:120].strip()}\n")

    print("\nIndexed files:")
    for fp in rag.indexed_files():
        print(f"  {fp}")

    # Cleanup test collection
    rag.clear()
