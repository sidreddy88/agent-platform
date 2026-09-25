"""Prompt caching in LLMService (opt-in) and its use by the ReAct loop."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.services.llm import LLMService, _mark_system_for_cache

EPHEMERAL = {"type": "ephemeral"}


def _svc() -> tuple[LLMService, AsyncMock]:
    usage = SimpleNamespace(input_tokens=1, output_tokens=1,
                            cache_read_input_tokens=0, cache_creation_input_tokens=0)
    create = AsyncMock(return_value=SimpleNamespace(usage=usage, content=[SimpleNamespace(text="ok")]))
    svc = LLMService.__new__(LLMService)
    svc._model = "claude-sonnet-4-6"
    svc._client = MagicMock()
    svc._client.messages.create = create
    return svc, create


def test_cache_marks_system_and_enables_automatic_caching():
    svc, create = _svc()
    asyncio.run(svc.complete([{"role": "user", "content": "hi"}], system="instructions", cache=True))
    kw = create.await_args.kwargs
    assert kw["cache_control"] == EPHEMERAL
    assert kw["system"] == [{"type": "text", "text": "instructions", "cache_control": EPHEMERAL}]


def test_no_cache_by_default():
    svc, create = _svc()
    asyncio.run(svc.complete([{"role": "user", "content": "hi"}], system="instructions"))
    kw = create.await_args.kwargs
    assert "cache_control" not in kw and kw["system"] == "instructions"


def test_existing_breakpoints_are_kept_and_input_not_mutated():
    harness = [
        {"type": "text", "text": "docs", "cache_control": EPHEMERAL},
        {"type": "text", "text": "prompt"},
    ]
    marked = _mark_system_for_cache(harness)
    assert marked[0]["cache_control"] == EPHEMERAL and marked[1]["cache_control"] == EPHEMERAL
    assert "cache_control" not in harness[1]
    # at most 3 of the 4 allowed breakpoints: docs + last system block + automatic
    assert sum("cache_control" in b for b in marked) == 2


def test_react_loop_asks_for_caching():
    from app.agents.base import BaseAgent

    class Agent(BaseAgent):
        pass

    llm = MagicMock()
    llm.complete = AsyncMock(return_value="Thought: done\nAnswer: 42")
    llm.last_input_tokens = llm.last_output_tokens = 0
    agent = Agent(llm=llm)
    asyncio.run(agent.run("q"))
    assert llm.complete.await_args.kwargs["cache"] is True
