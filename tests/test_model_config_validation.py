"""
Tests for the startup live-model check (app/services/model_config.py).

Why this file exists: the check was written after Anthropic silently retired a
dated snapshot in Aug 2026, which broke every agent built on the default
LLMService and went unnoticed because tracing was off and the test suite is
fully mocked. It then carried the same class of blind spot itself — anything
with a "/" in the id was skipped, so the review task's OpenAI model was never
verified. A typo'd or retired OpenAI id would 404 on every code review with
nothing warning at startup.

These cover both providers, and the cases where one provider's list is
unreachable but the other's isn't.
"""
from __future__ import annotations

import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.services import model_config


def _routing(**routes) -> dict:
    return {"routing": routes}


# ---------------------------------------------------------------------------
# _configured_models — collection and provider split
# ---------------------------------------------------------------------------

def test_splits_anthropic_and_openai_ids():
    cfg = _routing(
        diagnosis={"model": "claude-sonnet-5"},
        review={"model": "openai/gpt-5.5"},
        triage={"model": "claude-haiku-4-5", "fallback_model": "claude-sonnet-5"},
    )
    with patch.object(model_config, "_load_routing_config", return_value=cfg):
        anthropic, openai = model_config._configured_models()

    assert "claude-sonnet-5" in anthropic
    assert "claude-haiku-4-5" in anthropic
    # Prefix stripped — OpenAI's own model list uses the bare id.
    assert openai == {"gpt-5.5"}


def test_fallback_models_are_checked_too():
    """A dead fallback only surfaces during an outage — the worst time to find it."""
    cfg = _routing(triage={"model": "claude-haiku-4-5", "fallback_model": "claude-retired-9"})
    with patch.object(model_config, "_load_routing_config", return_value=cfg):
        anthropic, _ = model_config._configured_models()
    assert "claude-retired-9" in anthropic


def test_unknown_provider_prefixes_are_ignored():
    """No list to check against — guessing would produce false alarms."""
    cfg = _routing(x={"model": "together_ai/some-model"}, y={"model": "openai/gpt-5.5"})
    with patch.object(model_config, "_load_routing_config", return_value=cfg):
        anthropic, openai = model_config._configured_models()
    assert not any("together" in m for m in anthropic)
    assert openai == {"gpt-5.5"}


# ---------------------------------------------------------------------------
# validate_models_live — the actual gap that was closed
# ---------------------------------------------------------------------------

@pytest.fixture
def _prod_settings():
    with patch("app.core.config.settings") as s:
        s.environment = "production"
        s.anthropic_api_key = "sk-ant-test"
        s.openai_api_key = "sk-test"
        yield s


@pytest.mark.asyncio
async def test_dead_openai_model_is_reported(_prod_settings, caplog):
    """The regression this file is named for: a bad OpenAI id must not pass silently."""
    cfg = _routing(review={"model": "openai/gpt-nonexistent"})
    with patch.object(model_config, "_load_routing_config", return_value=cfg), \
         patch.object(model_config, "_live_anthropic_ids", AsyncMock(return_value=set())), \
         patch.object(model_config, "_live_openai_ids", AsyncMock(return_value={"gpt-5.5"})), \
         patch.object(model_config, "DEFAULT_MODELS", {}), \
         caplog.at_level(logging.ERROR):
        await model_config.validate_models_live()

    assert "openai/gpt-nonexistent" in caplog.text
    assert "404" in caplog.text


@pytest.mark.asyncio
async def test_dead_anthropic_model_still_reported(_prod_settings, caplog):
    cfg = _routing(diagnosis={"model": "claude-retired-9"})
    with patch.object(model_config, "_load_routing_config", return_value=cfg), \
         patch.object(model_config, "_live_anthropic_ids", AsyncMock(return_value={"claude-sonnet-5"})), \
         patch.object(model_config, "_live_openai_ids", AsyncMock(return_value=set())), \
         patch.object(model_config, "DEFAULT_MODELS", {}), \
         caplog.at_level(logging.ERROR):
        await model_config.validate_models_live()

    assert "claude-retired-9" in caplog.text


@pytest.mark.asyncio
async def test_all_live_logs_a_combined_count(_prod_settings, caplog):
    cfg = _routing(diagnosis={"model": "claude-sonnet-5"}, review={"model": "openai/gpt-5.5"})
    with patch.object(model_config, "_load_routing_config", return_value=cfg), \
         patch.object(model_config, "_live_anthropic_ids", AsyncMock(return_value={"claude-sonnet-5"})), \
         patch.object(model_config, "_live_openai_ids", AsyncMock(return_value={"gpt-5.5"})), \
         patch.object(model_config, "DEFAULT_MODELS", {}), \
         caplog.at_level(logging.INFO):
        await model_config.validate_models_live()

    assert "2 configured model(s) verified live" in caplog.text
    assert "no longer exist" not in caplog.text


@pytest.mark.asyncio
async def test_one_unreachable_provider_does_not_suppress_the_other(_prod_settings, caplog):
    """Providers are checked independently — an OpenAI outage must not hide a dead Claude id."""
    cfg = _routing(diagnosis={"model": "claude-retired-9"}, review={"model": "openai/gpt-5.5"})
    with patch.object(model_config, "_load_routing_config", return_value=cfg), \
         patch.object(model_config, "_live_anthropic_ids", AsyncMock(return_value={"claude-sonnet-5"})), \
         patch.object(model_config, "_live_openai_ids", AsyncMock(return_value=None)), \
         patch.object(model_config, "DEFAULT_MODELS", {}), \
         caplog.at_level(logging.ERROR):
        await model_config.validate_models_live()

    assert "claude-retired-9" in caplog.text
    # gpt-5.5 was unverifiable, not dead — it must not be reported as missing.
    assert "gpt-5.5" not in caplog.text


@pytest.mark.asyncio
async def test_never_raises_when_both_lists_are_unreachable(_prod_settings):
    """A validation failure must not take startup down."""
    cfg = _routing(diagnosis={"model": "claude-sonnet-5"}, review={"model": "openai/gpt-5.5"})
    with patch.object(model_config, "_load_routing_config", return_value=cfg), \
         patch.object(model_config, "_live_anthropic_ids", AsyncMock(return_value=None)), \
         patch.object(model_config, "_live_openai_ids", AsyncMock(return_value=None)), \
         patch.object(model_config, "DEFAULT_MODELS", {}):
        await model_config.validate_models_live()   # must not raise


@pytest.mark.asyncio
async def test_skipped_entirely_in_test_environment():
    with patch("app.core.config.settings") as s:
        s.environment = "test"
        anthropic_call = AsyncMock(return_value=set())
        with patch.object(model_config, "_live_anthropic_ids", anthropic_call):
            await model_config.validate_models_live()
        anthropic_call.assert_not_called()
