"""RAGService's chunk registry: scoped by collection, reset by clear(), and the per-service descriptions switch.
Real local Chroma and the test DB; embeddings and LLM descriptions faked."""
from __future__ import annotations

import pytest

from app.services import rag as rag_mod


def make(tmp_path, name, **kw):
    svc = rag_mod.RAGService(chroma_path=str(tmp_path / "chroma"), collection_name=name, openai_api_key="test", **kw)

    async def fake_embed(texts):
        return [[0.1] * 1536 for _ in texts]
    svc._embed = fake_embed
    return svc


def repo(tmp_path, name, body="def handler(x):\n    return x + 1\n"):
    d = tmp_path / name
    (d / "app").mkdir(parents=True)
    (d / "app" / "views.py").write_text(body)
    (d / "setup.py").write_text("from setuptools import setup\nsetup()\n")
    return d


@pytest.mark.asyncio
async def test_two_collections_with_the_same_paths_both_get_every_file(tmp_path):
    """The registry used to be keyed by path alone: a second collection holding the same unchanged files (another
    commit of the same repo, or another repo with a setup.py) skipped them all as 'already indexed'."""
    a, b = make(tmp_path, "case_one", function_descriptions=False), make(tmp_path, "case_two",
                                                                          function_descriptions=False)
    src = repo(tmp_path, "r")
    n_a = await a.index_directory(str(src), root=str(src))
    n_b = await b.index_directory(str(src), root=str(src))
    assert n_a > 0 and n_b == n_a
    assert b._collection.count() == a._collection.count() > 0
    assert await a.index_directory(str(src), root=str(src)) == 0       # unchanged, same collection: skipped


@pytest.mark.asyncio
async def test_clear_lets_the_same_collection_be_rebuilt(tmp_path):
    svc = make(tmp_path, "case_three", function_descriptions=False)
    src = repo(tmp_path, "r2")
    first = await svc.index_directory(str(src), root=str(src))
    svc.clear()
    assert svc._collection.count() == 0
    assert await svc.index_directory(str(src), root=str(src)) == first  # re-embedded, not skipped
    assert svc._collection.count() > 0


@pytest.mark.asyncio
async def test_descriptions_switch_controls_the_per_function_llm_calls(tmp_path, monkeypatch):
    calls = []

    async def fake_describe(llm, content):
        calls.append(content)
        return "describes it"
    monkeypatch.setattr(rag_mod, "_describe_with_limit", fake_describe)
    off = make(tmp_path, "case_off", function_descriptions=False)
    await off.index_directory(str(repo(tmp_path, "r3")), root=str(tmp_path / "r3"))
    assert calls == []
    on = make(tmp_path, "case_on", function_descriptions=True)
    await on.index_directory(str(repo(tmp_path, "r4")), root=str(tmp_path / "r4"))
    assert len(calls) >= 1


def test_default_collection_keeps_bare_paths_in_the_registry(tmp_path):
    assert make(tmp_path, rag_mod.COLLECTION_NAME)._registry_key("a/b.py") == "a/b.py"
    assert make(tmp_path, "codeidx_x_y")._registry_key("a/b.py") == "codeidx_x_y::a/b.py"
