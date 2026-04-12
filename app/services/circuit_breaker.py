"""
Circuit Breaker — protects external service calls from cascading failures.

Three states:
  CLOSED    Normal operation. Failures counted; opens when threshold reached.
  OPEN      Service considered down. All calls rejected with CircuitOpenError
            until timeout_seconds elapses.
  HALF_OPEN Testing recovery. One call allowed through; success closes the
            breaker, failure re-opens it immediately.

Applied to:
  "anthropic_llm" — LLMService.complete()
  "github_api"    — IncidentLoop._run_fix() / _run_review()

Usage:
    from app.services.circuit_breaker import circuit_breaker_registry

    cb = circuit_breaker_registry.get_or_create("my_service",
             failure_threshold=5, timeout_seconds=60.0)
    try:
        result = await cb.call(some_coroutine())
    except CircuitOpenError:
        # fast-fail path
        ...
"""
from __future__ import annotations

import logging
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# State enum + error
# ---------------------------------------------------------------------------

class CircuitState(str, Enum):
    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the circuit breaker is OPEN."""
    def __init__(self, name: str) -> None:
        super().__init__(f"Circuit breaker '{name}' is OPEN — call rejected")
        self.breaker_name = name


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreaker:
    """
    Single circuit breaker instance.

    Attributes:
        name                 Identifier (shown in API / logs).
        failure_threshold    Consecutive failures before opening.
        timeout_seconds      Seconds to wait in OPEN before trying HALF_OPEN.
        success_threshold    Consecutive successes in HALF_OPEN to close.
    """
    name:              str
    failure_threshold: int   = 5
    timeout_seconds:   float = 60.0
    success_threshold: int   = 2

    _state:          CircuitState = field(default=CircuitState.CLOSED, init=False, repr=False)
    _failure_count:  int          = field(default=0,   init=False, repr=False)
    _success_count:  int          = field(default=0,   init=False, repr=False)
    _opened_at:      float        = field(default=0.0, init=False, repr=False)

    # Cumulative observability counters
    total_calls:    int = field(default=0, init=False, repr=False)
    total_failures: int = field(default=0, init=False, repr=False)
    total_rejected: int = field(default=0, init=False, repr=False)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, coro: Coroutine) -> Any:
        """
        Attempt the coroutine under circuit-breaker protection.

        Raises CircuitOpenError immediately if the breaker is OPEN and the
        timeout has not yet elapsed.  Re-raises any exception from the
        coroutine itself after recording it as a failure.
        """
        self.total_calls += 1
        self._maybe_transition_half_open()

        if self._state == CircuitState.OPEN:
            self.total_rejected += 1
            raise CircuitOpenError(self.name)

        try:
            result = await coro
            self._on_success()
            return result
        except CircuitOpenError:
            raise
        except Exception as exc:
            self._on_failure(exc)
            raise

    def reset(self) -> None:
        """Manually force the breaker to CLOSED (e.g. after ops intervention)."""
        prev = self._state
        self._state         = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        logger.info("[CircuitBreaker] '%s' manually reset from %s → CLOSED", self.name, prev)

    def info(self) -> dict[str, Any]:
        """Return a JSON-serialisable snapshot of the breaker's current state."""
        return {
            "name":              self.name,
            "state":             self._state.value,
            "failure_count":     self._failure_count,
            "success_count":     self._success_count,
            "failure_threshold": self.failure_threshold,
            "timeout_seconds":   self.timeout_seconds,
            "success_threshold": self.success_threshold,
            "total_calls":       self.total_calls,
            "total_failures":    self.total_failures,
            "total_rejected":    self.total_rejected,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _maybe_transition_half_open(self) -> None:
        if (
            self._state == CircuitState.OPEN
            and time.monotonic() - self._opened_at >= self.timeout_seconds
        ):
            self._state         = CircuitState.HALF_OPEN
            self._success_count = 0
            logger.info("[CircuitBreaker] '%s' OPEN → HALF_OPEN (testing recovery)", self.name)

    def _on_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state         = CircuitState.CLOSED
                self._failure_count = 0
                logger.info("[CircuitBreaker] '%s' HALF_OPEN → CLOSED (recovered)", self.name)
        else:
            self._failure_count = 0   # reset streak on any success

    def _on_failure(self, exc: Exception) -> None:
        self.total_failures += 1
        self._failure_count += 1

        if self._state in (CircuitState.CLOSED, CircuitState.HALF_OPEN):
            if self._failure_count >= self.failure_threshold:
                self._state    = CircuitState.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "[CircuitBreaker] '%s' → OPEN after %d failures. Last: %s",
                    self.name, self._failure_count, exc,
                )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class CircuitBreakerRegistry:
    """
    Central store for all circuit breakers.  Breakers are created on first
    access and reused for the lifetime of the process.
    """

    def __init__(self) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}

    def get_or_create(
        self,
        name: str,
        *,
        failure_threshold: int   = 5,
        timeout_seconds:   float = 60.0,
        success_threshold: int   = 2,
    ) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                timeout_seconds=timeout_seconds,
                success_threshold=success_threshold,
            )
        return self._breakers[name]

    def get(self, name: str) -> CircuitBreaker | None:
        return self._breakers.get(name)

    def all_states(self) -> list[dict[str, Any]]:
        return [cb.info() for cb in self._breakers.values()]

    def reset(self, name: str) -> bool:
        """Reset a named breaker to CLOSED.  Returns False if not found."""
        cb = self._breakers.get(name)
        if cb is None:
            return False
        cb.reset()
        return True


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

circuit_breaker_registry = CircuitBreakerRegistry()
