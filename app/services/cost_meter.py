"""
Per-task LLM cost metering, split by billing type.

Open a meter around one unit of work (an eval case, an incident) and every
LLM call made inside it -- including calls from tools and nested agents --
is recorded against that meter:

    with cost_meter.metered() as meter:
        await agent.diagnose(incident)
    meter.summary()   # {"cost_usd": ..., "by_billing_type": {...}, ...}

Why this exists: the diagnosis gate ran out of Anthropic credits twice in
one day while every cost figure we had was an unmeasured estimate
(~$0.73/instance). LLMService recorded only input/output token counts and
dropped the cache fields entirely, even though BaseAgent sets cache_control,
so no existing number said what a task actually cost or how much of it was
cache traffic.

Scoped with a ContextVar, not a global: concurrent tasks (the gate's shards,
parallel incidents) each see only their own meter, and tasks spawned inside
a metered block inherit it. Outside any meter, record() is a no-op.
"""
from __future__ import annotations

import contextlib
import re
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

# $ per million tokens: (input, output). Anthropic first-party rates, checked
# 2026-09-25. Cache writes (5-min TTL) bill at 1.25x input, cache reads at
# 0.1x input. Keyed by alias; dated snapshot IDs are normalised below.
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10

_DATE_SUFFIX = re.compile(r"-\d{8}$")


def _price_key(model: str) -> str:
    return _DATE_SUFFIX.sub("", model.split("/")[-1])


@dataclass
class _ModelUsage:
    calls: int = 0
    input_tokens: int = 0          # uncached input, billed at the full rate
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def cost_by_billing_type(self, model: str) -> dict[str, float] | None:
        prices = PRICES_PER_MTOK.get(_price_key(model))
        if prices is None:
            return None
        inp, out = prices
        return {
            "input": self.input_tokens * inp / 1e6,
            "output": self.output_tokens * out / 1e6,
            "cache_write": self.cache_write_tokens * inp * CACHE_WRITE_MULTIPLIER / 1e6,
            "cache_read": self.cache_read_tokens * inp * CACHE_READ_MULTIPLIER / 1e6,
        }


@dataclass
class CostMeter:
    by_model: dict[str, _ModelUsage] = field(default_factory=dict)
    # One entry per LLM call, in order. Lets a caller attribute usage to the
    # exact call that incurred it (see scripts/analyze_cost_by_source.py).
    call_log: list[dict] = field(default_factory=list)

    def record(self, model: str, input_tokens: int, output_tokens: int,
               cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> None:
        self.call_log.append({
            "model": model, "input": input_tokens or 0, "output": output_tokens or 0,
            "cache_read": cache_read_tokens or 0, "cache_write": cache_write_tokens or 0,
        })
        u = self.by_model.setdefault(model, _ModelUsage())
        u.calls += 1
        u.input_tokens += input_tokens or 0
        u.output_tokens += output_tokens or 0
        u.cache_read_tokens += cache_read_tokens or 0
        u.cache_write_tokens += cache_write_tokens or 0

    def summary(self) -> dict:
        """JSON-safe totals. cost_usd is None if any model has no known price
        -- a partial total would read as the real one."""
        by_type = {"input": 0.0, "output": 0.0, "cache_write": 0.0, "cache_read": 0.0}
        unpriced = []
        tokens = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        calls = 0
        for model, u in self.by_model.items():
            calls += u.calls
            tokens["input"] += u.input_tokens
            tokens["output"] += u.output_tokens
            tokens["cache_write"] += u.cache_write_tokens
            tokens["cache_read"] += u.cache_read_tokens
            costs = u.cost_by_billing_type(model)
            if costs is None:
                unpriced.append(model)
                continue
            for k, v in costs.items():
                by_type[k] += v
        all_input = tokens["input"] + tokens["cache_write"] + tokens["cache_read"]
        return {
            "calls": calls,
            "models": sorted(self.by_model),
            "tokens": tokens,
            "cache_hit_rate": round(tokens["cache_read"] / all_input, 4) if all_input else None,
            "by_billing_type": {k: round(v, 6) for k, v in by_type.items()},
            "cost_usd": None if unpriced else round(sum(by_type.values()), 6),
            "unpriced_models": unpriced,
        }


# A stack, so meters nest: an eval case metered across all its attempts can
# contain a per-replay meter, and a call is recorded against both.
_current: ContextVar[tuple[CostMeter, ...]] = ContextVar("cost_meter", default=())


@contextlib.contextmanager
def metered() -> Iterator[CostMeter]:
    meter = CostMeter()
    token = _current.set(_current.get() + (meter,))
    try:
        yield meter
    finally:
        _current.reset(token)


def record(model: str, input_tokens: int, output_tokens: int,
           cache_read_tokens: int = 0, cache_write_tokens: int = 0) -> None:
    """Record one LLM call against every active meter. Never raises:
    metering must not be able to break an LLM call."""
    for meter in _current.get():
        try:
            meter.record(model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
        except Exception:
            pass
