"""verify_symbol_in_repo / _symbol_exists_in_repo: local checkout first, and a
failed lookup never reads as NOT_FOUND.

Before: the tool queried GitHub Code Search only. With an expired token (HTTP
401), search_code returned [] and the tool answered NOT_FOUND for 87 of 87
lookups in one eval run, 85 of them symbols that existed, each telling the
agent to drop the symbol and cap confidence."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.diagnosis import DiagnosisAgent
from app.services.github import GitHubSearchError

FILES = {
    "pkg/core.py": "class Collector:\n    def related_objects(self, objs):\n        return objs\n",
    "tests/test_core.py": "def related_objects():\n    pass\n",
    "pkg/use.py": "x = Collector().related_objects([])\n",
}


def _agent(files=None, ready=True, github=None):
    repo = MagicMock(ready=ready, pinned=False)
    repo.list_files.return_value = list(files or {})
    repo.read_file.side_effect = lambda p: (files or {})[p]
    return DiagnosisAgent(github=github or MagicMock(), local_repo=repo, owner="o", repo="r", rag=None)


def _verify(agent, symbol):
    return asyncio.run(agent._tools["verify_symbol_in_repo"][0](symbol=symbol))


def test_local_checkout_finds_definitions_source_first_and_normalises_names():
    out = _verify(_agent(FILES), "Collector.related_objects")
    lines = out.splitlines()
    assert lines[0].startswith("FOUND") and "bare name 'related_objects'" in lines[0]
    assert "pkg/core.py:2" in lines[1]                      # the source definition, not the test's
    assert any("tests/test_core.py" in line for line in lines[2:])


def test_local_miss_is_not_found_at_this_checkout():
    assert _verify(_agent(FILES), "no_such_symbol").startswith("NOT_FOUND: 'no_such_symbol' is not defined")


def test_github_failure_is_a_verify_error_never_not_found():
    gh = MagicMock()
    gh.search_code = AsyncMock(side_effect=GitHubSearchError("HTTP 401: Bad credentials"))
    out = _verify(_agent(ready=False, github=gh), "related_objects")
    assert out.startswith("VERIFY_ERROR") and "says nothing about whether" in out


def test_github_used_when_no_checkout_and_empty_results_are_not_found():
    gh = MagicMock()
    gh.search_code = AsyncMock(return_value=[])
    assert _verify(_agent(ready=False, github=gh), "x").startswith("NOT_FOUND: 'x' does not appear")
    assert gh.search_code.await_args.kwargs == {"strict": True}


def test_grounding_check_is_local_first_and_unknown_is_not_fabricated():
    agent = _agent(FILES)
    assert asyncio.run(agent._symbol_exists_in_repo("related_objects")) is True
    assert asyncio.run(agent._symbol_exists_in_repo("no_such_symbol")) is False
    gh = MagicMock()
    gh.search_code = AsyncMock(side_effect=GitHubSearchError("HTTP 401"))
    assert asyncio.run(_agent(ready=False, github=gh)._symbol_exists_in_repo("anything")) is True


@pytest.mark.parametrize("raw,bare", [("def foo(x)", "foo"), ("class Foo(Bar)", "Foo"),
                                      ("a.b.method", "method"), ("async function go", "go")])
def test_bare_symbol(raw, bare):
    assert DiagnosisAgent._bare_symbol(raw) == bare


def test_search_code_strict_raises_on_http_error_and_default_still_returns_empty():
    from unittest.mock import patch

    from app.services.github import GitHubService

    resp = MagicMock(status_code=401, text="Bad credentials")
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=client)
    cm.__aexit__ = AsyncMock(return_value=False)
    svc = GitHubService.__new__(GitHubService)
    with patch.object(GitHubService, "_client", return_value=cm):
        assert asyncio.run(svc.search_code("o", "r", "q")) == []
        with pytest.raises(GitHubSearchError, match="401"):
            asyncio.run(svc.search_code("o", "r", "q", strict=True))
