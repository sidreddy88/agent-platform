"""
Regression tests for BaseAgent's tool-call enforcement before accepting an answer:
_min_tool_calls_before_answer (count-based, PR #157) and
_required_tool_names_before_answer (name-based, added alongside DiagnosisAgent's
affected_file/root_cause_snippet grounding fix).

Real production bug the count-based floor alone can't catch: a diagnosis called
two log-checking tools (get_error_samples, check_still_occurring) -- both
returned no data -- satisfying "at least 1 real tool call," then answered with
a fabricated affected_file and root_cause_snippet, having never called a tool
that reads actual code. A count floor can't distinguish "verified something
real" from "called any tool, even one that found nothing relevant."
_required_tool_names_before_answer requires at least one call to a tool NAMED
in a given set (e.g. code-reading tools) before an answer is accepted, on top
of the count floor.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.base import BaseAgent


def _make_agent(**overrides) -> BaseAgent:
    agent = BaseAgent.__new__(BaseAgent)
    agent._llm = MagicMock()
    agent._gateway = None
    agent._tools = {}
    agent._tracing_ctx = MagicMock(trace=None, enabled=False)
    agent._harness_docs = ""
    agent._min_tool_calls_before_answer = 0
    agent._required_tool_names_before_answer = set()
    agent._must_call_before_answer = None
    agent._must_call_check = None
    for k, v in overrides.items():
        setattr(agent, k, v)
    return agent


@pytest.mark.asyncio
async def test_required_tool_names_rejects_answer_when_wrong_tool_called():
    """The actual real-world bug: a tool WAS called (satisfying any count
    floor), just not one of the required ones — must still be rejected."""
    agent = _make_agent(required_tool_names_before_answer_marker=None)
    agent._required_tool_names_before_answer = {"get_file_contents"}
    agent.register_tool("check_logs", AsyncMock(return_value="no data"), "checks logs")
    agent.register_tool("get_file_contents", AsyncMock(return_value="file content"), "reads a file")

    responses = [
        'Thought: checking logs\nAction: check_logs\nAction Input: {}',
        'Thought: I have enough\nAnswer: fabricated conclusion',
        'Thought: ok let me actually read the file\nAction: get_file_contents\nAction Input: {"path": "x.js"}',
        'Thought: now I really know\nAnswer: grounded conclusion',
    ]
    agent._llm.complete = AsyncMock(side_effect=responses)
    agent._llm.last_input_tokens = 10
    agent._llm.last_output_tokens = 10

    result = await agent.run("diagnose this")

    assert result.answer == "grounded conclusion"
    # The premature "fabricated conclusion" answer must have been rejected,
    # not returned — confirmed by the loop continuing to the real tool call.
    assert agent._llm.complete.call_count == 4


@pytest.mark.asyncio
async def test_required_tool_names_accepts_when_called_directly():
    agent = _make_agent()
    agent._required_tool_names_before_answer = {"get_file_contents", "search_codebase"}
    agent.register_tool("search_codebase", AsyncMock(return_value="found: x.js"), "searches code")

    responses = [
        'Thought: searching\nAction: search_codebase\nAction Input: {"query": "x"}',
        'Thought: found it\nAnswer: real conclusion',
    ]
    agent._llm.complete = AsyncMock(side_effect=responses)
    agent._llm.last_input_tokens = 10
    agent._llm.last_output_tokens = 10

    result = await agent.run("diagnose this")

    assert result.answer == "real conclusion"
    assert agent._llm.complete.call_count == 2


@pytest.mark.asyncio
async def test_required_tool_names_empty_set_preserves_existing_behavior():
    """Default (empty set) must not affect any agent that hasn't opted in —
    zero tool calls, immediate answer, same as before this change existed."""
    agent = _make_agent()  # _required_tool_names_before_answer defaults to set()
    agent._llm.complete = AsyncMock(return_value="Thought: done\nAnswer: immediate answer")
    agent._llm.last_input_tokens = 10
    agent._llm.last_output_tokens = 10

    result = await agent.run("anything")

    assert result.answer == "immediate answer"
    assert agent._llm.complete.call_count == 1


@pytest.mark.asyncio
async def test_required_tool_names_fails_closed_after_max_iterations():
    """Never calling a required tool across every allowed iteration must fall
    through to the same fail-closed path the count floor already uses — not
    silently accept the answer on the last try."""
    agent = _make_agent()
    agent._required_tool_names_before_answer = {"get_file_contents"}
    # Always tries to answer immediately, never calls any tool.
    agent._llm.complete = AsyncMock(return_value="Thought: done\nAnswer: never verified")
    agent._llm.last_input_tokens = 10
    agent._llm.last_output_tokens = 10

    with patch("app.agents.base.MAX_ITERATIONS", 3):
        result = await agent.run("diagnose this")

    assert result.answer != "never verified"
    assert "unable to find an answer" in result.answer.lower()
    assert agent._llm.complete.call_count == 3


@pytest.mark.asyncio
async def test_required_tool_names_message_takes_priority_over_count_message():
    """When both the count floor and the required-names set are unmet, the
    rejection should name the required tools (the more specific, more useful
    guidance) rather than just the generic count message."""
    agent = _make_agent()
    agent._min_tool_calls_before_answer = 1
    agent._required_tool_names_before_answer = {"get_file_contents"}
    agent._llm.complete = AsyncMock(
        side_effect=[
            "Thought: done\nAnswer: too fast",
            'Thought: ok\nAction: get_file_contents\nAction Input: {"path": "x.js"}',
            "Thought: now\nAnswer: real answer",
        ]
    )
    agent._llm.last_input_tokens = 10
    agent._llm.last_output_tokens = 10
    agent.register_tool("get_file_contents", AsyncMock(return_value="content"), "reads a file")

    result = await agent.run("diagnose this")

    assert result.answer == "real answer"
