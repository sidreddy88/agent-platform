"""find_callers answers from the call graph of the repo being diagnosed.

It used to answer every repo's question from the module-level graph, which is
the target app's (loaded from the store at import), so in every SWE-bench
replay find_callers searched a JavaScript app's graph while diagnosing a
Python repo."""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import app.agents.diagnosis as diagnosis_mod
from app.agents.diagnosis import DiagnosisAgent
from app.core.config import settings
from app.services.code_graph.graph import CallerInfo, CodeGraph


def _agent(owner, repo, ready=True, **kw):
    local = MagicMock(ready=ready, pinned=True)
    local.local_path = "/tmp/checkout"
    return DiagnosisAgent(github=MagicMock(), local_repo=local, owner=owner, repo=repo, rag=None, **kw)


def test_other_repo_builds_its_own_graph_from_the_checkout():
    built = CodeGraph()
    built.reverse["_separable"] = [CallerInfo("astropy/modeling/separable.py", "separability_matrix", 100)]
    agent = _agent("astropy", "astropy")
    with patch.object(CodeGraph, "build_from_directory", return_value=built) as build:
        asyncio.run(agent._ensure_code_graph())
    build.assert_called_once_with("/tmp/checkout")
    assert agent._code_graph is built and agent._code_graph is not diagnosis_mod._code_graph
    out = asyncio.run(agent._tools["find_callers"][0](function_name="_separable"))
    assert "astropy/modeling/separable.py" in out
    assert "astropy/modeling/separable.py" in agent._retrieved_file_paths


def test_target_app_keeps_the_stored_graph():
    owner, repo = settings.fix_target_repo.split("/", 1)
    agent = _agent(owner, repo)
    with patch.object(CodeGraph, "build_from_directory") as build:
        asyncio.run(agent._ensure_code_graph())
    build.assert_not_called()
    assert agent._code_graph is diagnosis_mod._code_graph


def test_no_checkout_means_an_empty_graph_not_the_target_apps():
    agent = _agent("astropy", "astropy", ready=False)
    asyncio.run(agent._ensure_code_graph())
    assert agent._code_graph is not diagnosis_mod._code_graph
    assert agent._code_graph.reverse == {}


def test_an_explicit_graph_is_used_as_is_and_built_once():
    given = CodeGraph()
    agent = _agent("astropy", "astropy", code_graph=given)
    with patch.object(CodeGraph, "build_from_directory") as build:
        asyncio.run(agent._ensure_code_graph())
        asyncio.run(agent._ensure_code_graph())
    build.assert_not_called()
    assert agent._code_graph is given
