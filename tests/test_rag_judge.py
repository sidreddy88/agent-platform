"""
Tests for app.services.rag_judge — sampled RAG faithfulness/relevance scoring.

All unit tests, no real LLM calls — the LLMGateway service is mocked.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.services.rag_judge import (
    JUDGE_SAMPLE_RATE,
    _strip_code_fence,
    judge_diagnosis_faithfulness,
    should_sample,
)


class TestShouldSample:
    def test_returns_bool(self):
        assert isinstance(should_sample(), bool)

    def test_rate_zero_never_samples(self):
        assert should_sample(rate=0.0) is False

    def test_rate_one_always_samples(self):
        assert should_sample(rate=1.0) is True

    def test_default_rate_is_ten_percent(self):
        assert JUDGE_SAMPLE_RATE == 0.10


class TestStripCodeFence:
    def test_strips_json_fence(self):
        raw = '```json\n{"a": 1}\n```'
        assert _strip_code_fence(raw) == '{"a": 1}'

    def test_strips_bare_fence(self):
        raw = '```\n{"a": 1}\n```'
        assert _strip_code_fence(raw) == '{"a": 1}'

    def test_passthrough_when_no_fence(self):
        raw = '{"a": 1}'
        assert _strip_code_fence(raw) == '{"a": 1}'


class TestJudgeDiagnosisFaithfulness:
    @pytest.mark.asyncio
    async def test_returns_none_with_no_chunks(self):
        result = await judge_diagnosis_faithfulness("q", [], "some diagnosis")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_with_no_answer(self):
        result = await judge_diagnosis_faithfulness("q", ["some context"], "")
        assert result is None

    @pytest.mark.asyncio
    async def test_parses_fenced_json_response(self):
        mock_service = AsyncMock()
        mock_service.complete.return_value = (
            '```json\n{"faithfulness": 0.9, "relevance": 0.8, "notes": "looks right"}\n```'
        )
        with patch("app.services.rag_judge.llm_gateway") as mock_gateway:
            mock_gateway.get_llm_service_for.return_value = mock_service
            result = await judge_diagnosis_faithfulness("incident text", ["context chunk"], "diagnosis text")

        assert result == {"faithfulness": 0.9, "relevance": 0.8, "notes": "looks right"}

    @pytest.mark.asyncio
    async def test_returns_none_on_malformed_response(self):
        mock_service = AsyncMock()
        mock_service.complete.return_value = "not json at all"
        with patch("app.services.rag_judge.llm_gateway") as mock_gateway:
            mock_gateway.get_llm_service_for.return_value = mock_service
            result = await judge_diagnosis_faithfulness("incident text", ["context chunk"], "diagnosis text")

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_llm_call_raises(self):
        mock_service = AsyncMock()
        mock_service.complete.side_effect = RuntimeError("provider down")
        with patch("app.services.rag_judge.llm_gateway") as mock_gateway:
            mock_gateway.get_llm_service_for.return_value = mock_service
            result = await judge_diagnosis_faithfulness("incident text", ["context chunk"], "diagnosis text")

        assert result is None
