"""
Tests for ContextCheckpointer — context window checkpointing.

Run:
    pytest tests/test_checkpoint.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.checkpoint import (
    CHECKPOINT_AT,
    CONTEXT_WINDOW,
    ContextCheckpointer,
    context_checkpointer,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_step(iteration=1, thought="t", action="tool_a", action_input="{}", observation="result"):
    from app.agents.base import Step
    s = Step(iteration=iteration)
    s.thought = thought
    s.action = action
    s.action_input = action_input
    s.observation = observation
    return s


def _msgs(*contents):
    """Build a minimal alternating messages list from content strings."""
    roles = ["user", "assistant"] * 10
    return [{"role": roles[i], "content": c} for i, c in enumerate(contents)]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_context_window_is_200k(self):
        assert CONTEXT_WINDOW == 200_000

    def test_checkpoint_at_is_70_percent(self):
        assert CHECKPOINT_AT == 0.70

    def test_default_limit(self):
        cp = ContextCheckpointer()
        assert cp._limit == 140_000


# ---------------------------------------------------------------------------
# needs_checkpoint
# ---------------------------------------------------------------------------

class TestNeedsCheckpoint:
    def test_below_threshold_returns_false(self):
        cp = ContextCheckpointer()
        assert cp.needs_checkpoint(139_999) is False

    def test_at_threshold_returns_true(self):
        cp = ContextCheckpointer()
        assert cp.needs_checkpoint(140_000) is True

    def test_above_threshold_returns_true(self):
        cp = ContextCheckpointer()
        assert cp.needs_checkpoint(180_000) is True

    def test_zero_tokens_returns_false(self):
        cp = ContextCheckpointer()
        assert cp.needs_checkpoint(0) is False

    def test_custom_threshold(self):
        cp = ContextCheckpointer(context_window=100_000, threshold=0.5)
        assert cp._limit == 50_000
        assert cp.needs_checkpoint(49_999) is False
        assert cp.needs_checkpoint(50_000) is True


# ---------------------------------------------------------------------------
# compress — structure of returned messages
# ---------------------------------------------------------------------------

class TestCompressStructure:
    @pytest.mark.asyncio
    async def test_returns_three_messages(self):
        cp = ContextCheckpointer()
        steps = [_make_step()]
        messages = _msgs("user input", "assistant turn", "Observation: result")

        with patch.object(cp, "_summarise", new=AsyncMock(return_value="summary text")):
            result = await cp.compress(messages, steps)

        assert len(result) == 3

    @pytest.mark.asyncio
    async def test_first_message_is_original_user(self):
        cp = ContextCheckpointer()
        messages = _msgs("original request", "asst", "obs")

        with patch.object(cp, "_summarise", new=AsyncMock(return_value="s")):
            result = await cp.compress(messages, [_make_step()])

        assert result[0]["role"] == "user"
        assert result[0]["content"] == "original request"

    @pytest.mark.asyncio
    async def test_second_message_is_checkpoint_assistant(self):
        cp = ContextCheckpointer()
        messages = _msgs("req", "asst", "obs")

        with patch.object(cp, "_summarise", new=AsyncMock(return_value="found X and Y")):
            result = await cp.compress(messages, [_make_step()])

        assert result[1]["role"] == "assistant"
        assert "[CONTEXT CHECKPOINT" in result[1]["content"]
        assert "found X and Y" in result[1]["content"]

    @pytest.mark.asyncio
    async def test_third_message_is_last_observation(self):
        cp = ContextCheckpointer()
        last_obs = "Observation: the final result"
        messages = _msgs("req", "asst1", "obs1", "asst2", last_obs)

        with patch.object(cp, "_summarise", new=AsyncMock(return_value="s")):
            result = await cp.compress(messages, [_make_step()])

        assert result[-1]["role"] == "user"
        assert result[-1]["content"] == last_obs

    @pytest.mark.asyncio
    async def test_alternating_roles_maintained(self):
        cp = ContextCheckpointer()
        messages = _msgs("req", "asst", "obs")

        with patch.object(cp, "_summarise", new=AsyncMock(return_value="s")):
            result = await cp.compress(messages, [_make_step()])

        roles = [m["role"] for m in result]
        assert roles == ["user", "assistant", "user"]

    @pytest.mark.asyncio
    async def test_single_message_returned_unchanged(self):
        cp = ContextCheckpointer()
        messages = [{"role": "user", "content": "only"}]

        result = await cp.compress(messages, [])
        assert result == messages

    @pytest.mark.asyncio
    async def test_checkpoint_counter_increments(self):
        cp = ContextCheckpointer()
        assert cp.checkpoints_taken == 0

        with patch.object(cp, "_summarise", new=AsyncMock(return_value="s")):
            await cp.compress(_msgs("req", "asst", "obs"), [_make_step()])

        assert cp.checkpoints_taken == 1

    @pytest.mark.asyncio
    async def test_step_count_in_checkpoint_header(self):
        cp = ContextCheckpointer()
        steps = [_make_step(i) for i in range(1, 6)]
        messages = _msgs("req", "asst", "obs")

        with patch.object(cp, "_summarise", new=AsyncMock(return_value="s")):
            result = await cp.compress(messages, steps)

        assert "5" in result[1]["content"]  # "5 prior step(s) compressed"


# ---------------------------------------------------------------------------
# compress — summarisation fallback
# ---------------------------------------------------------------------------

class TestCompressFallback:
    @pytest.mark.asyncio
    async def test_fallback_when_summarise_raises(self):
        cp = ContextCheckpointer()
        step = _make_step(observation="key data found")
        messages = _msgs("req", "asst", "obs")

        with patch.object(cp, "_summarise", new=AsyncMock(side_effect=Exception("LLM error"))):
            # Should not raise — fallback activates
            result = await cp.compress(messages, [step])

        assert len(result) == 3  # structure still valid


# ---------------------------------------------------------------------------
# _format_steps / _fallback_summary
# ---------------------------------------------------------------------------

class TestFormatSteps:
    def test_empty_steps(self):
        text = ContextCheckpointer._format_steps([])
        assert text == ""

    def test_includes_iteration_number(self):
        step = _make_step(iteration=3, action="search", observation="found it")
        text = ContextCheckpointer._format_steps([step])
        assert "Iteration 3" in text
        assert "search" in text
        assert "found it" in text

    def test_truncates_long_observation(self):
        step = _make_step(observation="x" * 1000)
        text = ContextCheckpointer._format_steps([step])
        # Max observation in formatted text is 400 chars
        assert len(text) < 1000

    def test_fallback_summary_uses_observations(self):
        steps = [_make_step(iteration=1, action="lookup", observation="result A")]
        summary = ContextCheckpointer._fallback_summary(steps)
        assert "result A" in summary
        assert "lookup" in summary

    def test_fallback_summary_no_observations(self):
        step = _make_step(observation="")
        summary = ContextCheckpointer._fallback_summary([step])
        assert "no observations" in summary


# ---------------------------------------------------------------------------
# LLMService.last_input_tokens side-effect
# ---------------------------------------------------------------------------

class TestLLMServiceTokenTracking:
    def test_last_input_tokens_initialised_to_zero(self):
        from app.services.llm import LLMService
        llm = LLMService.__new__(LLMService)
        llm._model = "test"
        llm._client = MagicMock()
        llm.last_input_tokens = 0
        assert llm.last_input_tokens == 0

    @pytest.mark.asyncio
    async def test_last_input_tokens_set_after_complete(self):
        from app.services.llm import LLMService

        fake_response = MagicMock()
        fake_response.content = [MagicMock(text="hello")]
        fake_response.usage.input_tokens = 42_000

        llm = LLMService.__new__(LLMService)
        llm._model = "test"
        llm.last_input_tokens = 0
        llm._client = MagicMock()
        llm._client.messages.create = AsyncMock(return_value=fake_response)

        result = await llm.complete(messages=[{"role": "user", "content": "hi"}])
        assert result == "hello"
        assert llm.last_input_tokens == 42_000


# ---------------------------------------------------------------------------
# BaseAgent integration — checkpoint triggered during run()
# ---------------------------------------------------------------------------

class TestBaseAgentCheckpointIntegration:
    @pytest.mark.asyncio
    async def test_checkpoint_triggered_when_tokens_exceed_limit(self):
        """
        When the LLM reports high input_tokens, compress() is called and the
        agent continues with a shorter messages list.
        """
        from app.agents.base import BaseAgent
        from app.services.checkpoint import ContextCheckpointer

        call_count = 0

        async def _fake_complete(messages, system=None, tracing_ctx=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First call — return an action
                return "Thought: look it up\nAction: dummy\nAction Input: {}"
            # Second call — answer
            return "Thought: done\nAnswer: finished"

        agent = BaseAgent.__new__(BaseAgent)
        agent._tracing_ctx = MagicMock(enabled=False)
        agent._min_tool_calls_before_answer = 0  # __new__() skips __init__()'s default
        agent._required_tool_names_before_answer = set()  # same reason
        agent._must_call_before_answer = None  # same reason
        agent._must_call_check = None  # same reason
        agent._tools = {
            "dummy": (AsyncMock(return_value="result"), "dummy tool")
        }
        from app.services.llm import LLMService
        mock_llm = MagicMock(spec=LLMService)
        mock_llm.complete = _fake_complete
        mock_llm.last_input_tokens = 0
        mock_llm.last_output_tokens = 0  # BaseAgent.run() reads this to total output tokens
        agent._llm = mock_llm

        # Custom checkpointer with a very low limit so it fires immediately
        cp = ContextCheckpointer(context_window=1_000, threshold=0.5)  # limit = 500

        compress_called = False

        async def _fake_compress(messages, steps):
            nonlocal compress_called
            compress_called = True
            # Return a trimmed list
            return [messages[0], {"role": "assistant", "content": "[CP]"}, messages[-1]]

        cp.compress = _fake_compress

        # After first call, pretend tokens are above threshold
        async def _complete_with_tokens(messages, system=None, tracing_ctx=None):
            result = await _fake_complete(messages, system, tracing_ctx)
            mock_llm.last_input_tokens = 600  # above 500 limit
            return result

        mock_llm.complete = _complete_with_tokens

        with patch("app.agents.base.context_checkpointer", cp):
            # BaseAgent.run is decorated with @trace_agent; call underlying method directly
            await BaseAgent.run.__wrapped__(agent, "test input")

        assert compress_called, "compress() should have been called"

    @pytest.mark.asyncio
    async def test_no_checkpoint_when_tokens_low(self):
        """When token count stays low, compress() is never called."""
        from app.agents.base import BaseAgent
        from app.services.checkpoint import ContextCheckpointer

        async def _fake_complete(messages, system=None, tracing_ctx=None):
            return "Thought: done\nAnswer: finished"

        agent = BaseAgent.__new__(BaseAgent)
        agent._tracing_ctx = MagicMock(enabled=False)
        agent._min_tool_calls_before_answer = 0  # __new__() skips __init__()'s default
        agent._required_tool_names_before_answer = set()  # same reason
        agent._must_call_before_answer = None  # same reason
        agent._must_call_check = None  # same reason
        agent._tools = {}
        from app.services.llm import LLMService
        mock_llm = MagicMock(spec=LLMService)
        mock_llm.complete = _fake_complete
        mock_llm.last_input_tokens = 100  # well below any threshold
        mock_llm.last_output_tokens = 0  # BaseAgent.run() reads this to total output tokens
        agent._llm = mock_llm

        cp = ContextCheckpointer(context_window=200_000, threshold=0.70)
        compress_called = False

        async def _fake_compress(messages, steps):
            nonlocal compress_called
            compress_called = True
            return messages

        cp.compress = _fake_compress

        with patch("app.agents.base.context_checkpointer", cp):
            await BaseAgent.run.__wrapped__(agent, "hi")

        assert not compress_called


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_is_instance(self):
        assert isinstance(context_checkpointer, ContextCheckpointer)

    def test_default_limit(self):
        assert context_checkpointer._limit == 140_000
