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


def test_gateway_llm_service_accepts_cache_and_forwards_it():
    """Production agents run on GatewayLLMService (incident_loop swaps it in);
    BaseAgent.run passing cache=True must not break it."""
    from app.services.llm_gateway import GatewayLLMService, LLMResponse

    gw = MagicMock()
    gw.complete = AsyncMock(return_value=LLMResponse(
        content="ok", input_tokens=1, output_tokens=1, provider="anthropic",
        model="claude-sonnet-4-6", cost_usd=0.0))
    svc = GatewayLLMService(gw, "diagnosis")
    assert asyncio.run(svc.complete([{"role": "user", "content": "hi"}], system="s", cache=True)) == "ok"
    assert gw.complete.await_args.kwargs["cache"] is True


def test_litellm_path_marks_system_and_latest_message():
    from app.services.llm_gateway import _mark_for_cache

    history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
               {"role": "user", "content": "observation"}]
    system, msgs = _mark_for_cache("instructions", history)
    assert system[-1]["cache_control"] == EPHEMERAL
    assert msgs[-1]["content"] == [{"type": "text", "text": "observation", "cache_control": EPHEMERAL}]
    assert msgs[:-1] == history[:-1]
    assert history[-1]["content"] == "observation"  # caller's history untouched


def test_litellm_provider_sends_breakpoints_only_when_asked():
    from unittest.mock import patch

    from app.services.llm_gateway import LiteLLMProvider

    resp = SimpleNamespace(usage=None, choices=[SimpleNamespace(message=SimpleNamespace(content="x"))])
    with patch("litellm.acompletion", new=AsyncMock(return_value=resp)) as acomp:
        asyncio.run(LiteLLMProvider().complete([{"role": "user", "content": "hi"}],
                                               "claude-sonnet-4-6", system="s", cache=True))
        sent = acomp.await_args.kwargs["messages"]
        assert sent[0]["content"][-1]["cache_control"] == EPHEMERAL
        assert sent[-1]["content"][-1]["cache_control"] == EPHEMERAL
        asyncio.run(LiteLLMProvider().complete([{"role": "user", "content": "hi"}],
                                               "claude-sonnet-4-6", system="s"))
        sent = acomp.await_args.kwargs["messages"]
        assert sent == [{"role": "system", "content": "s"}, {"role": "user", "content": "hi"}]
