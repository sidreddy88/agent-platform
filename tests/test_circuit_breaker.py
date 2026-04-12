"""
Tests for CircuitBreaker + CircuitBreakerRegistry + failure injection API.

Run:
    pytest tests/test_circuit_breaker.py -v
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.services.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    CircuitOpenError,
    CircuitState,
    circuit_breaker_registry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _ok():
    return "success"


async def _fail():
    raise RuntimeError("boom")


# ---------------------------------------------------------------------------
# CircuitState
# ---------------------------------------------------------------------------

class TestCircuitState:
    def test_values(self):
        assert CircuitState.CLOSED    == "closed"
        assert CircuitState.OPEN      == "open"
        assert CircuitState.HALF_OPEN == "half_open"


# ---------------------------------------------------------------------------
# CircuitOpenError
# ---------------------------------------------------------------------------

class TestCircuitOpenError:
    def test_message_contains_name(self):
        exc = CircuitOpenError("my_service")
        assert "my_service" in str(exc)
        assert exc.breaker_name == "my_service"

    def test_is_runtime_error(self):
        assert isinstance(CircuitOpenError("x"), RuntimeError)


# ---------------------------------------------------------------------------
# CircuitBreaker — CLOSED state (normal operation)
# ---------------------------------------------------------------------------

class TestCircuitBreakerClosed:
    @pytest.mark.asyncio
    async def test_successful_call_returns_result(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        result = await cb.call(_ok())
        assert result == "success"

    @pytest.mark.asyncio
    async def test_failed_call_re_raises(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        with pytest.raises(RuntimeError, match="boom"):
            await cb.call(_fail())

    @pytest.mark.asyncio
    async def test_state_stays_closed_below_threshold(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        for _ in range(2):   # 2 failures, threshold is 3
            with pytest.raises(RuntimeError):
                await cb.call(_fail())
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_opens_after_reaching_failure_threshold(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(_fail())
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_success_resets_consecutive_failure_count(self):
        cb = CircuitBreaker(name="test", failure_threshold=3)
        with pytest.raises(RuntimeError):
            await cb.call(_fail())   # failure 1
        with pytest.raises(RuntimeError):
            await cb.call(_fail())
        await cb.call(_ok())   # success — resets streak
        # Now we need 3 more failures to open
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail())
        assert cb.state == CircuitState.CLOSED  # still closed (only 2 since reset)

    @pytest.mark.asyncio
    async def test_counters_updated(self):
        cb = CircuitBreaker(name="test", failure_threshold=5)
        await cb.call(_ok())
        with pytest.raises(RuntimeError):
            await cb.call(_fail())
        assert cb.total_calls == 2
        assert cb.total_failures == 1
        assert cb.total_rejected == 0


# ---------------------------------------------------------------------------
# CircuitBreaker — OPEN state
# ---------------------------------------------------------------------------

class TestCircuitBreakerOpen:
    @pytest.mark.asyncio
    async def test_open_rejects_calls_immediately(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, timeout_seconds=9999)
        with pytest.raises(RuntimeError):
            await cb.call(_fail())   # trips the breaker
        assert cb.state == CircuitState.OPEN

        with pytest.raises(CircuitOpenError):
            await cb.call(_ok())   # rejected without calling _ok

    @pytest.mark.asyncio
    async def test_rejected_counter_increments(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, timeout_seconds=9999)
        with pytest.raises(RuntimeError):
            await cb.call(_fail())
        for _ in range(3):
            with pytest.raises(CircuitOpenError):
                await cb.call(_ok())
        assert cb.total_rejected == 3

    @pytest.mark.asyncio
    async def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, timeout_seconds=0.0)
        with pytest.raises(RuntimeError):
            await cb.call(_fail())
        assert cb.state == CircuitState.OPEN
        # timeout_seconds=0.0 → any subsequent call triggers HALF_OPEN
        result = await cb.call(_ok())
        assert result == "success"
        # After 1 success (success_threshold=2 by default), still HALF_OPEN
        assert cb.state == CircuitState.HALF_OPEN


# ---------------------------------------------------------------------------
# CircuitBreaker — HALF_OPEN state
# ---------------------------------------------------------------------------

class TestCircuitBreakerHalfOpen:
    def _open_breaker(self, cb: CircuitBreaker) -> None:
        """Force breaker into OPEN state synchronously."""
        cb._state = CircuitState.OPEN
        cb._opened_at = 0.0   # ensures timeout elapsed immediately

    @pytest.mark.asyncio
    async def test_success_in_half_open_increments_success_count(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, success_threshold=2, timeout_seconds=0.0)
        self._open_breaker(cb)
        await cb.call(_ok())   # triggers HALF_OPEN on entry
        assert cb.state == CircuitState.HALF_OPEN
        assert cb._success_count == 1

    @pytest.mark.asyncio
    async def test_closes_after_success_threshold_met(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, success_threshold=2, timeout_seconds=0.0)
        self._open_breaker(cb)
        await cb.call(_ok())   # first success in HALF_OPEN
        await cb.call(_ok())   # second success → CLOSED
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failure_in_half_open_reopens(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, success_threshold=2, timeout_seconds=0.0)
        self._open_breaker(cb)
        await cb.call(_ok())   # enters HALF_OPEN
        with pytest.raises(RuntimeError):
            await cb.call(_fail())   # failure → back to OPEN
        assert cb.state == CircuitState.OPEN


# ---------------------------------------------------------------------------
# CircuitBreaker — reset
# ---------------------------------------------------------------------------

class TestCircuitBreakerReset:
    @pytest.mark.asyncio
    async def test_reset_closes_open_breaker(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, timeout_seconds=9999)
        with pytest.raises(RuntimeError):
            await cb.call(_fail())
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_reset_clears_failure_count(self):
        cb = CircuitBreaker(name="test", failure_threshold=5)
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb.call(_fail())
        cb.reset()
        assert cb._failure_count == 0

    @pytest.mark.asyncio
    async def test_calls_succeed_after_reset(self):
        cb = CircuitBreaker(name="test", failure_threshold=1, timeout_seconds=9999)
        with pytest.raises(RuntimeError):
            await cb.call(_fail())
        cb.reset()
        result = await cb.call(_ok())
        assert result == "success"


# ---------------------------------------------------------------------------
# CircuitBreaker — info()
# ---------------------------------------------------------------------------

class TestCircuitBreakerInfo:
    def test_info_has_required_keys(self):
        cb = CircuitBreaker(name="svc")
        info = cb.info()
        for key in ("name", "state", "failure_count", "total_calls", "total_failures", "total_rejected"):
            assert key in info

    def test_info_name_matches(self):
        cb = CircuitBreaker(name="my_service")
        assert cb.info()["name"] == "my_service"

    def test_info_state_is_string(self):
        cb = CircuitBreaker(name="svc")
        assert isinstance(cb.info()["state"], str)


# ---------------------------------------------------------------------------
# CircuitBreakerRegistry
# ---------------------------------------------------------------------------

class TestCircuitBreakerRegistry:
    def test_get_or_create_returns_same_instance(self):
        reg = CircuitBreakerRegistry()
        cb1 = reg.get_or_create("svc")
        cb2 = reg.get_or_create("svc")
        assert cb1 is cb2

    def test_different_names_different_instances(self):
        reg = CircuitBreakerRegistry()
        cb1 = reg.get_or_create("svc_a")
        cb2 = reg.get_or_create("svc_b")
        assert cb1 is not cb2

    def test_get_returns_none_for_unknown(self):
        reg = CircuitBreakerRegistry()
        assert reg.get("does_not_exist") is None

    def test_get_returns_existing(self):
        reg = CircuitBreakerRegistry()
        cb = reg.get_or_create("known")
        assert reg.get("known") is cb

    def test_all_states_returns_list(self):
        reg = CircuitBreakerRegistry()
        reg.get_or_create("a")
        reg.get_or_create("b")
        states = reg.all_states()
        names = {s["name"] for s in states}
        assert "a" in names
        assert "b" in names

    def test_reset_unknown_returns_false(self):
        reg = CircuitBreakerRegistry()
        assert reg.reset("no_such_breaker") is False

    def test_reset_known_returns_true(self):
        reg = CircuitBreakerRegistry()
        reg.get_or_create("svc")
        assert reg.reset("svc") is True


# ---------------------------------------------------------------------------
# LLMService — circuit breaker integration
# ---------------------------------------------------------------------------

class TestLLMCircuitBreakerIntegration:
    @pytest.mark.asyncio
    async def test_llm_success_passes_through(self):
        from app.services.llm import LLMService
        from unittest.mock import MagicMock

        fake_response = MagicMock()
        fake_response.content = [MagicMock(text="hello")]
        fake_response.usage.input_tokens = 100

        llm = LLMService.__new__(LLMService)
        llm._model = "test"
        llm.last_input_tokens = 0
        llm._client = MagicMock()
        llm._client.messages.create = AsyncMock(return_value=fake_response)

        reg = CircuitBreakerRegistry()
        with patch("app.services.llm.circuit_breaker_registry", reg):
            result = await llm.complete([{"role": "user", "content": "hi"}])
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_llm_failure_tracked_by_breaker(self):
        from app.services.llm import LLMService
        from unittest.mock import MagicMock

        llm = LLMService.__new__(LLMService)
        llm._model = "test"
        llm.last_input_tokens = 0
        llm._client = MagicMock()
        llm._client.messages.create = AsyncMock(side_effect=RuntimeError("API down"))

        reg = CircuitBreakerRegistry()
        with patch("app.services.llm.circuit_breaker_registry", reg):
            for _ in range(5):
                with pytest.raises(RuntimeError):
                    await llm.complete([{"role": "user", "content": "hi"}])

        cb = reg.get("anthropic_llm")
        assert cb is not None
        assert cb.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_breaker_raises_circuit_open_error(self):
        from app.services.llm import LLMService
        from unittest.mock import MagicMock

        llm = LLMService.__new__(LLMService)
        llm._model = "test"
        llm.last_input_tokens = 0
        llm._client = MagicMock()
        llm._client.messages.create = AsyncMock(side_effect=RuntimeError("API down"))

        reg = CircuitBreakerRegistry()
        with patch("app.services.llm.circuit_breaker_registry", reg):
            for _ in range(5):
                with pytest.raises(RuntimeError):
                    await llm.complete([{"role": "user", "content": "hi"}])
            # Now open — next call should raise CircuitOpenError
            with pytest.raises(CircuitOpenError):
                await llm.complete([{"role": "user", "content": "hi"}])


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestSingleton:
    def test_is_instance(self):
        assert isinstance(circuit_breaker_registry, CircuitBreakerRegistry)
