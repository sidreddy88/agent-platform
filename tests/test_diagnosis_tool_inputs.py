"""DiagnosisAgent tool inputs found broken by a SWE-bench trajectory
(matplotlib__matplotlib-22865):

  - get_file_contents could only ever return a file's first 12,000 chars,
    so code past the cut was unreachable -> line-range reads.
  - grep_codebase defaulted to '*.js', so on Python repos an unscoped grep
    searched nothing -> default '*'.
  - BaseAgent turned malformed JSON tool input into a raw string argument,
    silently -> an error the model can act on.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

from app.agents.base import BaseAgent
from app.agents.diagnosis import _FILE_READ_CHAR_LIMIT, DiagnosisAgent


def _agent_with_files(files: dict[str, str]) -> DiagnosisAgent:
    agent = DiagnosisAgent.__new__(DiagnosisAgent)
    agent._owner, agent._repo = "owner", "repo"
    agent._aws = MagicMock()
    agent._rag = None
    agent._github = MagicMock()
    agent._local_repo = MagicMock(ready=True, pinned=True)
    agent._local_repo.read_file.side_effect = lambda p: files[p]
    agent._local_repo.list_files.return_value = list(files)
    agent._retrieved_file_paths = set()
    agent._last_retrieved_chunks = []
    return agent


def _tool(agent: DiagnosisAgent, name: str):
    captured = {}

    def fake_register_tool(self, tool_name, fn, description):
        captured[tool_name] = fn

    with patch.object(DiagnosisAgent, "register_tool", fake_register_tool):
        DiagnosisAgent._register_tools(agent)
    return captured[name]


BIG = "".join(f"x = {i}  # padding padding padding padding\n" for i in range(1, 3001))


def test_large_file_is_cut_at_a_line_and_says_which_lines():
    agent = _agent_with_files({"big.py": BIG})
    out = asyncio.run(_tool(agent, "get_file_contents")(file_path="big.py"))
    assert "x = 1 " in out and "x = 3000 " not in out
    assert "Showing lines 1-" in out and "of 3000 in big.py" in out
    assert "start_line=" in out


def test_range_read_reaches_code_past_the_cap():
    agent = _agent_with_files({"big.py": BIG})
    out = asyncio.run(_tool(agent, "get_file_contents")(
        file_path="big.py", start_line=2500, end_line=2502))
    assert "x = 2500 " in out and "x = 2502 " in out and "x = 2503 " not in out
    assert "Showing lines 2500-2502 of 3000" in out
    assert "big.py" in agent._retrieved_file_paths


def test_range_read_is_itself_capped():
    agent = _agent_with_files({"big.py": BIG})
    out = asyncio.run(_tool(agent, "get_file_contents")(
        file_path="big.py", start_line=1, end_line=3000))
    assert len(out) < _FILE_READ_CHAR_LIMIT + 1000
    assert "To read further" in out


def test_small_file_is_returned_whole_without_notice():
    agent = _agent_with_files({"small.py": "def f():\n    return 1\n"})
    out = asyncio.run(_tool(agent, "get_file_contents")(file_path="small.py"))
    assert "return 1" in out and "Showing lines" not in out


def test_invalid_range_is_reported():
    agent = _agent_with_files({"small.py": "a\nb\n"})
    out = asyncio.run(_tool(agent, "get_file_contents")(
        file_path="small.py", start_line=5, end_line=2))
    assert out.startswith("Invalid range")


def test_grep_default_searches_non_js_files():
    agent = _agent_with_files({
        "lib/colorbar.py": "class Colorbar:\n    def _add_solids(self):\n        pass\n",
        "web/app.js": "const x = 1;\n",
    })
    out = asyncio.run(_tool(agent, "grep_codebase")(pattern="_add_solids"))
    assert "lib/colorbar.py:2:" in out


def test_grep_glob_still_narrows():
    agent = _agent_with_files({"a.py": "needle\n", "b.js": "needle\n"})
    out = asyncio.run(_tool(agent, "grep_codebase")(pattern="needle", file_glob="*.js"))
    assert "b.js" in out and "a.py" not in out


class _Bare(BaseAgent):
    def __init__(self):  # skip LLM/tracing setup; only _execute_tool is under test
        self._tools = {}
        self._tracing_ctx = None


def test_malformed_json_input_returns_an_actionable_error():
    agent = _Bare()
    calls = []

    async def grep(pattern, file_glob="*"):
        calls.append(pattern)
        return "ran"

    agent._tools["grep_codebase"] = (grep, "")
    out = asyncio.run(agent._execute_tool(
        "grep_codebase", '{"pattern": "def _edges\\|x", "file_glob": "*.py"}'))
    assert calls == []
    assert out.startswith("Error: Action Input for 'grep_codebase' is not valid JSON")


def test_plain_string_input_still_passes_through():
    agent = _Bare()

    async def echo(text):
        return f"got {text}"

    agent._tools["echo"] = (echo, "")
    assert asyncio.run(agent._execute_tool("echo", "hello")) == "got hello"


def test_live_mode_reads_the_local_clone_not_github():
    """#232: github.get_file_contents hardcodes ref="main" and 404s on repos
    whose default branch is master (the target app), so a ready clone wins
    in live mode too, not only in pinned replay."""
    from unittest.mock import AsyncMock

    agent = _agent_with_files({"app.js": "const live = 1;\n"})
    agent._local_repo.pinned = False
    agent._github.get_file_contents = AsyncMock(side_effect=AssertionError("GitHub should not be called"))
    out = asyncio.run(_tool(agent, "get_file_contents")(file_path="app.js"))
    assert "const live = 1;" in out


def test_falls_back_to_github_when_local_read_fails():
    from unittest.mock import AsyncMock

    agent = _agent_with_files({})
    agent._local_repo.pinned = False
    agent._local_repo.read_file.side_effect = FileNotFoundError("not in clone")
    agent._github.get_file_contents = AsyncMock(return_value=("from github\n", "sha"))
    out = asyncio.run(_tool(agent, "get_file_contents")(file_path="x.js"))
    assert "from github" in out


def test_uses_github_when_no_clone_is_ready():
    from unittest.mock import AsyncMock

    agent = _agent_with_files({})
    agent._local_repo.ready = False
    agent._github.get_file_contents = AsyncMock(return_value=("remote\n", "sha"))
    out = asyncio.run(_tool(agent, "get_file_contents")(file_path="x.js"))
    assert "remote" in out and not agent._local_repo.read_file.called
