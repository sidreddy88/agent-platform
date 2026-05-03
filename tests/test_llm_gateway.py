"""
Unit tests for LLMGateway:
  - LLMResponse cost calculation
  - Routing config loading
  - LiteLLMProvider response normalization
  - complete_with_fallback trigger on low confidence
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.llm_gateway import (
    GatewayLLMService,
    LiteLLMProvider,
    LLMGateway,
    LLMResponse,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ROUTING_CONFIG = {
    "routing": {
        "triage":    {"model": "claude-haiku-4-5-20251001", "fallback_model": "claude-sonnet-4-6"},
        "diagnosis": {"model": "claude-sonnet-4-6"},
    },
    "cost_per_1k_tokens": {
        "claude-haiku-4-5-20251001": {"input": 0.00025, "output": 0.00125},
        "claude-sonnet-4-6":         {"input": 0.003,   "output": 0.015},
    },
}


def _make_gateway(config: dict | None = None) -> LLMGateway:
    """Create a gateway with an in-memory config, skipping disk I/O."""
    cfg = config if config is not None else ROUTING_CONFIG
    gw = LLMGateway.__new__(LLMGateway)
    gw._config = cfg
    gw._provider = MagicMock(spec=LiteLLMProvider)
    gw._daily_costs = {}
    return gw


# ---------------------------------------------------------------------------
# 1. LLMResponse cost calculation
# ---------------------------------------------------------------------------

class TestLLMResponseCostCalculation:
    def test_haiku_cost(self):
        gw = _make_gateway()
        cost = gw._compute_cost("claude-haiku-4-5-20251001", input_tokens=1000, output_tokens=500)
        # (1000 * 0.00025 + 500 * 0.00125) / 1000
        assert abs(cost - (1000 * 0.00025 + 500 * 0.00125) / 1000) < 1e-10

    def test_sonnet_cost(self):
        gw = _make_gateway()
        cost = gw._compute_cost("claude-sonnet-4-6", input_tokens=2000, output_tokens=1000)
        expected = (2000 * 0.003 + 1000 * 0.015) / 1000
        assert abs(cost - expected) < 1e-10

    def test_unknown_model_returns_zero(self):
        gw = _make_gateway()
        cost = gw._compute_cost("unknown-model", input_tokens=1000, output_tokens=500)
        assert cost == 0.0

    def test_zero_tokens(self):
        gw = _make_gateway()
        cost = gw._compute_cost("claude-sonnet-4-6", input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_costs_today_accumulates(self):
        gw = _make_gateway()
        gw._record_cost("triage", "anthropic", 0.001)
        gw._record_cost("triage", "anthropic", 0.002)
        gw._record_cost("diagnosis", "anthropic", 0.005)
        report = gw.costs_today()
        assert abs(report["llm_costs_today_usd"] - 0.008) < 1e-10
        assert abs(report["cost_by_task_type"]["triage"] - 0.003) < 1e-10
        assert abs(report["cost_by_task_type"]["diagnosis"] - 0.005) < 1e-10
        assert abs(report["cost_by_provider"]["anthropic"] - 0.008) < 1e-10


# ---------------------------------------------------------------------------
# 2. Routing config loading
# ---------------------------------------------------------------------------

class TestRoutingConfig:
    def test_loads_provider_and_model(self):
        gw = _make_gateway()
        provider, model = gw._get_routing("triage")
        assert provider == "anthropic"  # inferred from "claude-haiku-*"
        assert model == "claude-haiku-4-5-20251001"

    def test_unknown_task_type_uses_defaults(self):
        gw = _make_gateway()
        provider, model = gw._get_routing("unknown_task")
        assert provider == "unknown"  # empty model string → can't infer provider
        assert model == "claude-sonnet-4-6"

    def test_missing_config_file_returns_empty(self):
        result = LLMGateway._load_config(Path("/nonexistent/path.json"))
        assert result == {}

    def test_switching_triage_model_via_config(self):
        """Config change alone should switch the model — no code change required."""
        cfg = {**ROUTING_CONFIG, "routing": {
            **ROUTING_CONFIG["routing"],
            "triage": {"provider": "openai", "model": "gpt-4o-mini"},
        }}
        gw = _make_gateway(cfg)
        provider, model = gw._get_routing("triage")
        assert provider == "openai"
        assert model == "gpt-4o-mini"


# ---------------------------------------------------------------------------
# 3. LiteLLMProvider response normalization
# ---------------------------------------------------------------------------

def _make_litellm_response(content: str, prompt_tokens: int, completion_tokens: int) -> MagicMock:
    """Build a minimal litellm-style response object."""
    mock_usage = MagicMock(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)
    mock_message = MagicMock(content=content)
    mock_choice = MagicMock(message=mock_message)
    return MagicMock(choices=[mock_choice], usage=mock_usage)


class TestLiteLLMProviderNormalization:
    @pytest.mark.asyncio
    async def test_complete_returns_llm_response(self):
        mock_resp = _make_litellm_response("Hello, world!", 100, 50)

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            provider = LiteLLMProvider()
            result = await provider.complete(
                messages=[{"role": "user", "content": "hi"}],
                model="claude-haiku-4-5-20251001",
                system="You are helpful.",
            )

        assert isinstance(result, LLMResponse)
        assert result.content == "Hello, world!"
        assert result.input_tokens == 100
        assert result.output_tokens == 50
        assert result.provider == "anthropic"
        assert result.model == "claude-haiku-4-5-20251001"
        assert result.cost_usd == 0.0  # set by gateway, not provider

    @pytest.mark.asyncio
    async def test_system_prompt_prepended_as_message(self):
        """System prompt becomes the first message in the list passed to litellm."""
        mock_resp = _make_litellm_response("ok", 10, 5)

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)) as mock_call:
            provider = LiteLLMProvider()
            await provider.complete(
                messages=[{"role": "user", "content": "hi"}],
                model="claude-haiku-4-5-20251001",
                system="Be concise.",
            )

        sent_messages = mock_call.call_args.kwargs["messages"]
        assert sent_messages[0] == {"role": "system", "content": "Be concise."}
        assert sent_messages[1] == {"role": "user", "content": "hi"}

    @pytest.mark.asyncio
    async def test_openai_model_inferred_as_openai_provider(self):
        mock_resp = _make_litellm_response("done", 20, 10)

        with patch("litellm.acompletion", new=AsyncMock(return_value=mock_resp)):
            provider = LiteLLMProvider()
            result = await provider.complete(
                messages=[{"role": "user", "content": "hi"}],
                model="gpt-4.1",
            )

        assert result.provider == "openai"
        assert result.model == "gpt-4.1"


# ---------------------------------------------------------------------------
# 4. complete_with_fallback — triggers on low confidence
# ---------------------------------------------------------------------------

class TestFallbackTrigger:
    @pytest.mark.asyncio
    async def test_no_fallback_when_confidence_above_threshold(self):
        gw = _make_gateway()
        primary_resp = LLMResponse(
            content='{"result": "noise", "confidence": 0.85}',
            input_tokens=100, output_tokens=50,
            provider="anthropic", model="claude-haiku-4-5-20251001", cost_usd=0.001,
        )
        gw.complete = AsyncMock(return_value=primary_resp)

        result = await gw.complete_with_fallback(
            messages=[{"role": "user", "content": "triage this"}],
            task_type="triage",
            confidence_threshold=0.70,
        )

        assert result is primary_resp
        assert gw.complete.call_count == 1

    @pytest.mark.asyncio
    async def test_fallback_triggers_when_confidence_below_threshold(self):
        gw = _make_gateway()
        primary_resp = LLMResponse(
            content='{"result": "real", "confidence": 0.45}',
            input_tokens=100, output_tokens=50,
            provider="anthropic", model="claude-haiku-4-5-20251001", cost_usd=0.001,
        )
        fallback_resp = LLMResponse(
            content='{"result": "real", "confidence": 0.92}',
            input_tokens=200, output_tokens=80,
            provider="anthropic", model="claude-sonnet-4-6", cost_usd=0.002,
        )

        call_count = 0

        async def mock_complete(messages, task_type, **kwargs):
            nonlocal call_count
            call_count += 1
            return primary_resp

        gw.complete = mock_complete
        gw._call_provider = AsyncMock(return_value=fallback_resp)

        result = await gw.complete_with_fallback(
            messages=[{"role": "user", "content": "triage this"}],
            task_type="triage",
            confidence_threshold=0.70,
        )

        assert result.model == "claude-sonnet-4-6"
        assert call_count == 1
        assert gw._call_provider.called

    @pytest.mark.asyncio
    async def test_no_fallback_when_no_confidence_in_response(self):
        gw = _make_gateway()
        primary_resp = LLMResponse(
            content="This is noise.",
            input_tokens=50, output_tokens=20,
            provider="anthropic", model="claude-haiku-4-5-20251001", cost_usd=0.0,
        )
        gw.complete = AsyncMock(return_value=primary_resp)

        result = await gw.complete_with_fallback(
            messages=[{"role": "user", "content": "triage this"}],
            task_type="triage",
        )

        assert result is primary_resp

    @pytest.mark.asyncio
    async def test_no_fallback_when_no_fallback_model_configured(self):
        gw = _make_gateway()
        primary_resp = LLMResponse(
            content='{"result": "real", "confidence": 0.40}',
            input_tokens=100, output_tokens=50,
            provider="anthropic", model="claude-sonnet-4-6", cost_usd=0.001,
        )
        gw.complete = AsyncMock(return_value=primary_resp)

        result = await gw.complete_with_fallback(
            messages=[{"role": "user", "content": "diagnose this"}],
            task_type="diagnosis",  # no fallback_model in config
            confidence_threshold=0.70,
        )

        assert result is primary_resp


# ---------------------------------------------------------------------------
# 5. GatewayLLMService — LLMService compatibility
# ---------------------------------------------------------------------------

class TestGatewayLLMService:
    @pytest.mark.asyncio
    async def test_complete_returns_str_and_sets_token_counts(self):
        gw = _make_gateway()
        gw.complete = AsyncMock(return_value=LLMResponse(
            content="answer text",
            input_tokens=120, output_tokens=60,
            provider="anthropic", model="claude-haiku-4-5-20251001", cost_usd=0.0001,
        ))

        svc = GatewayLLMService(gw, "triage")
        result = await svc.complete(
            messages=[{"role": "user", "content": "hi"}],
            system="You are helpful.",
        )

        assert result == "answer text"
        assert svc.last_input_tokens == 120
        assert svc.last_output_tokens == 60

    def test_get_llm_service_for_returns_gateway_service(self):
        gw = _make_gateway()
        svc = gw.get_llm_service_for("triage")
        assert isinstance(svc, GatewayLLMService)
        assert svc._task_type == "triage"
