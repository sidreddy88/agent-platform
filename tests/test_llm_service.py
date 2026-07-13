"""
Tests for LLMService's retry-with-backoff+jitter layer (app/services/llm.py).

Run:
    pytest tests/test_llm_service.py -v
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import anthropic
import httpx
import pytest

from app.services.llm import _is_retryable, _retry_with_backoff

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUEST = httpx.Request("POST", "http://test/v1/messages")


def _status_error(cls: type[anthropic.APIStatusError], status_code: int) -> anthropic.APIStatusError:
    response = httpx.Response(status_code=status_code, request=_REQUEST)
    return cls(f"error {status_code}", response=response, body=None)


# ---------------------------------------------------------------------------
# _is_retryable — matches the retryable/non-retryable table
# ---------------------------------------------------------------------------

class TestIsRetryable:
    def test_rate_limit_429_is_retryable(self):
        assert _is_retryable(_status_error(anthropic.RateLimitError, 429)) is True

    def test_service_unavailable_503_is_retryable(self):
        assert _is_retryable(_status_error(anthropic.InternalServerError, 503)) is True

    def test_overloaded_529_is_retryable(self):
        # Anthropic raises 529 as its own OverloadedError, a private subclass of
        # APIStatusError (not InternalServerError, not exported publicly). Classifying
        # by status_code catches it without depending on that private class name.
        assert _is_retryable(_status_error(anthropic.APIStatusError, 529)) is True

    def test_connection_error_is_retryable(self):
        assert _is_retryable(anthropic.APIConnectionError(request=_REQUEST)) is True

    def test_timeout_error_is_retryable(self):
        assert _is_retryable(anthropic.APITimeoutError(request=_REQUEST)) is True

    def test_auth_failure_401_is_not_retryable(self):
        assert _is_retryable(_status_error(anthropic.AuthenticationError, 401)) is False

    def test_bad_request_400_is_not_retryable(self):
        assert _is_retryable(_status_error(anthropic.BadRequestError, 400)) is False

    def test_non_anthropic_error_is_not_retryable(self):
        assert _is_retryable(RuntimeError("boom")) is False


# ---------------------------------------------------------------------------
# _retry_with_backoff
# ---------------------------------------------------------------------------

class TestRetryWithBackoff:
    @pytest.mark.asyncio
    async def test_succeeds_without_retry_when_no_error(self):
        call_fn = AsyncMock(return_value="ok")
        result = await _retry_with_backoff(call_fn)
        assert result == "ok"
        assert call_fn.call_count == 1

    @pytest.mark.asyncio
    async def test_retries_on_retryable_error_then_succeeds(self):
        call_fn = AsyncMock(
            side_effect=[_status_error(anthropic.RateLimitError, 429), "ok"]
        )
        with patch("app.services.llm.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            result = await _retry_with_backoff(call_fn)
        assert result == "ok"
        assert call_fn.call_count == 2
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gives_up_after_max_retries(self):
        call_fn = AsyncMock(side_effect=_status_error(anthropic.InternalServerError, 503))
        with patch("app.services.llm.asyncio.sleep", new=AsyncMock()):
            with pytest.raises(anthropic.InternalServerError):
                await _retry_with_backoff(call_fn)
        assert call_fn.call_count == 4   # 1 initial + 3 retries

    @pytest.mark.asyncio
    async def test_non_retryable_error_raises_immediately(self):
        call_fn = AsyncMock(side_effect=_status_error(anthropic.BadRequestError, 400))
        with patch("app.services.llm.asyncio.sleep", new=AsyncMock()) as mock_sleep:
            with pytest.raises(anthropic.BadRequestError):
                await _retry_with_backoff(call_fn)
        assert call_fn.call_count == 1
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_anthropic_error_propagates_without_retry(self):
        call_fn = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            await _retry_with_backoff(call_fn)
        assert call_fn.call_count == 1
