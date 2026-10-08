"""
The fix agent's test loop: with a sandbox attached it can run commands in the
repo's environment (its current edits written in first) and discard its edits
to try another approach. Without a sandbox (production) nothing changes.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents import fix_generation
from app.agents.fix_generation import FixGenerationAgent
from app.models.events import ErrorEvent, EventSource, IncidentState
from app.services.test_sandbox import swebench_image, trim_output

CONTENT = "function target(x) {\n  return x.value;\n}\n"
FIXED = "function target(x) {\n  return x ? x.value : null;\n}"


class FakeSandbox:
    def __init__(self):
        self.files: list[tuple[str, str]] = []
        self.commands: list[str] = []

    async def write_file(self, path, content):
        self.files.append((path, content))

    async def run(self, command, timeout=180):
        self.commands.append(command)
        return (1 if "repro" in command and len(self.commands) == 1 else 0), f"ran: {command}"

    async def close(self):
        pass


def _agent(replies, sandbox=None):
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner, agent._repo, agent._github = "o", "r", MagicMock()
    agent._with_harness = MagicMock(return_value="(harness)")
    if sandbox is not None:
        agent._sandbox = sandbox
    seen, calls = [], []
    queue = list(replies)

    async def fake(messages=None, tools=None, system=None, **kwargs):
        seen.append(list(messages))
        calls.append([t["name"] for t in tools or []])
        return queue.pop(0) if queue else ("", [], "end_turn")

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake)
    return agent, seen, calls


async def _run(agent):
    incident = IncidentState(error_event=ErrorEvent(source=EventSource.APPLICATION, error_type="TypeError",
                                                    title="x", description="y", service="s"))
    return await agent._generate_fix(content=CONTENT, function_name="target", incident=incident,
                                     file_path="src/target.js", context_bundle={"callers": [], "tests": [], "imports": []})


def _tool(name, **inputs):
    return ("", [{"id": name, "name": name, "input": inputs}], "tool_use")


@pytest.mark.asyncio
async def test_no_sandbox_means_no_test_tools_and_no_test_prompt():
    agent, seen, calls = _agent([_tool("apply_edit", new_text=FIXED), ("done", [], "end_turn")])
    await _run(agent)
    assert "run_command" not in calls[0] and "reset_edits" not in calls[0]
    assert "TEST LOOP" not in seen[0][0]["content"]


@pytest.mark.asyncio
async def test_run_command_writes_current_edits_first_and_is_logged():
    sb = FakeSandbox()
    agent, seen, calls = _agent([
        _tool("run_command", command="python /tmp/repro.py"),       # before the edit
        _tool("apply_edit", new_text=FIXED),
        _tool("run_command", command="python /tmp/repro.py"),       # after the edit
        ("done", [], "end_turn"),
    ], sandbox=sb)
    _, new, _, _ = await _run(agent)
    assert new == FIXED
    assert "run_command" in calls[0] and "TEST LOOP" in seen[0][0]["content"]
    assert sb.files[0] == ("src/target.js", CONTENT)                # original file before editing
    assert FIXED in sb.files[1][1]                                  # edited file afterwards
    log = agent._test_log
    assert [r["edited"] for r in log] == [False, True] and log[0]["exit"] == 1 and log[1]["exit"] == 0
    tool_msgs = [m["content"] for m in seen[-1] if m.get("role") == "tool"]
    assert any("exit code: 1" in t for t in tool_msgs)


@pytest.mark.asyncio
async def test_reset_edits_discards_and_allows_a_new_primary_edit():
    other = "function target(x) {\n  return (x || {}).value;\n}"
    sb = FakeSandbox()
    agent, seen, calls = _agent([
        _tool("apply_edit", new_text=FIXED),
        _tool("reset_edits", reason="tests show the guard is wrong"),
        _tool("apply_edit", new_text=other),
        ("done", [], "end_turn"),
    ], sandbox=sb)
    _, new, _, _ = await _run(agent)
    assert new == other
    assert agent._test_log[-1]["command"] == "<reset_edits>"


@pytest.mark.asyncio
async def test_sandbox_gets_the_larger_turn_budget():
    reads = [_tool("read_file", path="src/other.js")] * (fix_generation._MAX_FIX_TURNS_TEST_LOOP + 5)
    agent, seen, calls = _agent(reads, sandbox=FakeSandbox())
    agent._read_file = AsyncMock(return_value=("const other = 1;", "sha"))
    await _run(agent)
    assert len(calls) == fix_generation._MAX_FIX_TURNS_TEST_LOOP


def test_trim_output_keeps_start_and_end():
    text = "A" * 3000 + "MIDDLE" + "Z" * 5000
    out = trim_output(text)
    assert out.startswith("A" * 100) and out.endswith("Z" * 100) and "MIDDLE" not in out and "omitted" in out


def test_swebench_image_name():
    assert swebench_image("django__django-11099") == "docker.io/swebench/sweb.eval.x86_64.django_1776_django-11099:latest"


@pytest.mark.asyncio
async def test_answer_hunting_commands_are_refused_and_not_run():
    sb = FakeSandbox()
    agent, seen, calls = _agent([
        _tool("run_command", command="git log --all --oneline"),
        _tool("apply_edit", new_text=FIXED),
        _tool("run_command", command="python /tmp/repro.py"),
        ("done", [], "end_turn"),
    ], sandbox=sb)
    await _run(agent)
    assert sb.commands == ["python /tmp/repro.py"]
    assert agent._test_log[0]["exit"] == "refused"
    assert any("REFUSED" in m["content"] for msgs in seen for m in msgs if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_finishing_without_testing_the_edit_gets_one_verify_nudge():
    sb = FakeSandbox()
    agent, seen, calls = _agent([
        _tool("apply_edit", new_text=FIXED),
        ("done", [], "end_turn"),                                    # tries to finish untested
        _tool("run_command", command="python /tmp/repro.py"),
        ("done", [], "end_turn"),
    ], sandbox=sb)
    _, new, _, _ = await _run(agent)
    assert new == FIXED and sb.commands == ["python /tmp/repro.py"]
    assert "haven't run anything since your last edit" in seen[2][-1]["content"]


@pytest.mark.asyncio
async def test_no_verify_nudge_without_a_sandbox():
    agent, seen, calls = _agent([_tool("apply_edit", new_text=FIXED), ("done", [], "end_turn")])
    await _run(agent)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_edit_by_warning_leaves_turns_to_test():
    reads = [_tool("read_file", path="src/other.js")] * fix_generation._MAX_FIX_TURNS_TEST_LOOP
    agent, seen, calls = _agent(reads, sandbox=FakeSandbox())
    agent._read_file = AsyncMock(return_value=("const other = 1;", "sha"))
    await _run(agent)
    k = fix_generation._MAX_FIX_TURNS_TEST_LOOP - fix_generation._TEST_LOOP_EDIT_BY_TURNS
    assert "turns left and no edit yet" in seen[k][-1]["content"]


class ReproSandbox(FakeSandbox):
    """`python /tmp/repro.py` exits 1 unless the last file written contains `fixed_marker`."""

    def __init__(self, fixed_marker="x ? x.value"):
        super().__init__()
        self.marker = fixed_marker

    async def run(self, command, timeout=180):
        self.commands.append(command)
        if "repro" in command:
            fixed = bool(self.files) and self.marker in self.files[-1][1]
            return (0 if fixed else 1), ("ok" if fixed else "AssertionError")
        return 0, "ran"


@pytest.mark.asyncio
async def test_reproduction_accepted_only_if_it_fails_on_the_unfixed_file():
    sb = ReproSandbox()
    agent, seen, calls = _agent([
        _tool("set_reproduction", command="python /tmp/repro.py"),
        _tool("apply_edit", new_text=FIXED),
        ("done", [], "end_turn"),
    ], sandbox=sb)
    _, new, _, _ = await _run(agent)
    assert new == FIXED
    assert sb.files[0] == ("src/target.js", CONTENT)                     # run on the unfixed file
    log = [r["command"] for r in agent._test_log]
    assert log[0].startswith("<repro on unfixed>") and log[-1] == "<repro on final edit>"
    assert agent._test_log[-1]["exit"] == 0


@pytest.mark.asyncio
async def test_reproduction_that_passes_on_unfixed_code_is_rejected():
    sb = ReproSandbox(fixed_marker="return x.value")                      # "passes" already
    agent, seen, calls = _agent([
        _tool("set_reproduction", command="python /tmp/repro.py"),
        ("done", [], "end_turn"),
    ], sandbox=sb)
    await _run(agent)
    tool_msgs = [m["content"] for m in seen[1] if m.get("role") == "tool"]
    assert any("REJECTED" in t and "doesn't capture the bug" in t for t in tool_msgs)


@pytest.mark.asyncio
async def test_final_check_sends_the_model_back_while_the_reproduction_fails():
    wrong = "function target(x) {\n  return x.value || null;\n}"
    sb = ReproSandbox()
    agent, seen, calls = _agent([
        _tool("set_reproduction", command="python /tmp/repro.py"),
        _tool("apply_edit", new_text=wrong),
        ("done", [], "end_turn"),                                         # final check fails
        _tool("reset_edits", reason="still failing"),
        _tool("apply_edit", new_text=FIXED),
        ("done", [], "end_turn"),                                         # final check passes
    ], sandbox=sb)
    _, new, _, _ = await _run(agent)
    assert new == FIXED
    assert "still fails with your edit" in seen[3][-1]["content"]
    finals = [r for r in agent._test_log if r["command"] == "<repro on final edit>"]
    assert [r["exit"] for r in finals] == [1, 0]


@pytest.mark.asyncio
async def test_reproduce_early_nudge():
    reads = [_tool("read_file", path="src/other.js")] * 12
    agent, seen, calls = _agent(reads, sandbox=FakeSandbox())
    agent._read_file = AsyncMock(return_value=("const other = 1;", "sha"))
    await _run(agent)
    k = fix_generation._REPRO_BY_TURN
    assert "haven't registered a reproduction" in seen[k][-1]["content"]
