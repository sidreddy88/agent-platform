import pytest
"""LLM_MODEL_<TASK> overrides one task's routed model for a process (evals)."""
from app.services.llm_gateway import llm_gateway


def test_env_override_replaces_the_routed_model(monkeypatch):
    _, routed, routed_max_tokens = llm_gateway._get_routing("diagnosis")
    monkeypatch.setenv("LLM_MODEL_DIAGNOSIS", "together_ai/Qwen/Qwen3-Coder-Next-FP8")
    provider, model, max_tokens = llm_gateway._get_routing("diagnosis")
    assert model == "together_ai/Qwen/Qwen3-Coder-Next-FP8" and provider == "together_ai"
    assert max_tokens == routed_max_tokens                  # the rest of the routing entry is kept
    monkeypatch.delenv("LLM_MODEL_DIAGNOSIS")
    assert llm_gateway._get_routing("diagnosis")[1] == routed


def test_open_model_prices_are_known():
    from app.services.cost_meter import PRICES_PER_MTOK, _price_key
    from scripts.scout_open_models import MODELS
    assert all(_price_key(m) in PRICES_PER_MTOK for m in MODELS)


def test_provider_specific_cache_read_price():
    from app.services.cost_meter import _ModelUsage
    u = _ModelUsage(cache_read_tokens=1_000_000)
    assert u.cost_by_billing_type("together_ai/deepseek-ai/DeepSeek-V4.1-Flash")["cache_read"] == pytest.approx(0.006)
    assert u.cost_by_billing_type("claude-sonnet-5")["cache_read"] == pytest.approx(0.20)


def test_cost_by_source_handles_models_with_their_own_cache_price():
    """r8 crashed here: the analysis unpacked (input, output) and Together
    prices carry a third, cached-input price."""
    from scripts.analyze_cost_by_source import attribute_call
    usage = [{"model": "together_ai/deepseek-ai/DeepSeek-V4.1-Flash", "input": 100,
              "cache_read": 1000, "cache_write": 0, "output": 50}]
    out = attribute_call([("task_prompt", 400), ("history", 700)], usage)
    assert "_unpriced" not in out and out["model_output"]
