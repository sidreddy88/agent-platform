"""Per-task cost metering (app/services/cost_meter.py) and its two hooks:
LLMService (raw SDK) and LLMGateway."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import cost_meter


def test_prices_each_billing_type():
    with cost_meter.metered() as m:
        cost_meter.record("claude-sonnet-4-6", 1_000_000, 100_000, 2_000_000, 400_000)
    s = m.summary()
    assert s["by_billing_type"] == {
        "input": 3.0,            # 1M uncached x $3
        "output": 1.5,           # 100k x $15
        "cache_write": 1.5,      # 400k x $3 x 1.25
        "cache_read": 0.6,       # 2M x $3 x 0.1
    }
    assert s["cost_usd"] == 6.6
    assert s["cache_hit_rate"] == round(2_000_000 / 3_400_000, 4)


def test_dated_snapshot_and_provider_prefix_resolve_to_alias_price():
    with cost_meter.metered() as m:
        cost_meter.record("claude-haiku-4-5-20251001", 1_000_000, 0)
        cost_meter.record("anthropic/claude-haiku-4-5", 1_000_000, 0)
    assert m.summary()["cost_usd"] == 2.0


def test_unknown_model_makes_total_unknown_not_partial():
    with cost_meter.metered() as m:
        cost_meter.record("claude-sonnet-4-6", 1_000_000, 0)
        cost_meter.record("gpt-5.5", 1_000_000, 0)
    s = m.summary()
    assert s["cost_usd"] is None
    assert s["unpriced_models"] == ["gpt-5.5"]


def test_record_outside_a_meter_is_a_noop():
    cost_meter.record("claude-sonnet-4-6", 10, 10)  # must not raise


def test_meters_are_isolated_between_concurrent_tasks():
    async def work(n):
        with cost_meter.metered() as m:
            for _ in range(n):
                await asyncio.sleep(0)
                cost_meter.record("claude-sonnet-4-6", 1, 1)
        return m.summary()["calls"]

    async def main():
        return await asyncio.gather(work(3), work(5))

    assert asyncio.run(main()) == [3, 5]


def test_child_tasks_inherit_the_meter():
    async def child():
        cost_meter.record("claude-sonnet-4-6", 1, 1)

    async def main():
        with cost_meter.metered() as m:
            await asyncio.gather(child(), child())
        return m.summary()["calls"]

    assert asyncio.run(main()) == 2


def test_llm_service_records_cache_fields():
    from app.services.llm import LLMService

    usage = SimpleNamespace(input_tokens=100, output_tokens=20,
                            cache_read_input_tokens=900, cache_creation_input_tokens=50)
    response = SimpleNamespace(usage=usage, content=[SimpleNamespace(text="ok")])
    svc = LLMService.__new__(LLMService)
    svc._model = "claude-sonnet-4-6"
    svc._client = MagicMock()
    svc._client.messages.create = AsyncMock(return_value=response)

    with cost_meter.metered() as m:
        assert asyncio.run(svc.complete([{"role": "user", "content": "hi"}])) == "ok"
    t = m.summary()["tokens"]
    assert t == {"input": 100, "output": 20, "cache_write": 50, "cache_read": 900}


@pytest.mark.asyncio
async def test_gateway_records_uncached_input_separately():
    from app.services.llm_gateway import LLMGateway, LLMResponse

    gw = LLMGateway.__new__(LLMGateway)
    gw._config = {"cost_per_1k_tokens": {}}
    gw._daily_costs = {}
    gw._provider = MagicMock()
    raw = LLMResponse(content="x", input_tokens=1000, output_tokens=10, provider="anthropic",
                      model="claude-sonnet-4-6", cost_usd=0.0,
                      cache_read_input_tokens=700, cache_creation_input_tokens=100)
    with patch.object(LLMGateway, "_get_routing", return_value=("anthropic", "claude-sonnet-4-6", 4096)), \
         patch.object(LLMGateway, "_call_provider", AsyncMock(return_value=raw)):
        with cost_meter.metered() as m:
            await gw.complete([{"role": "user", "content": "hi"}], "diagnosis")
    # LiteLLM's prompt_tokens includes reads AND writes (verified live)
    assert m.summary()["tokens"] == {"input": 200, "output": 10, "cache_write": 100, "cache_read": 700}


def test_nested_meters_both_record_and_inner_is_scoped():
    with cost_meter.metered() as outer:
        cost_meter.record("claude-sonnet-4-6", 1, 1)
        with cost_meter.metered() as inner:
            cost_meter.record("claude-sonnet-4-6", 2, 2)
        cost_meter.record("claude-sonnet-4-6", 3, 3)
    assert [c["input"] for c in outer.call_log] == [1, 2, 3]
    assert [c["input"] for c in inner.call_log] == [2]
