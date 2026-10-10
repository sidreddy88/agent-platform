"""Phase D acceptance options: cost compared only on shared successes, and
near misses (in-band positive candidates) kept for the proposer."""
from __future__ import annotations

from app.harness_optimizer.acceptance import AcceptanceConfig, CaseResult, EvalResult, decide, paired_deltas


def _res(spec):
    """spec: case -> (passes, trials, cost)"""
    return EvalResult({c: CaseResult(passes=p, trials=t, cost_usd=cost) for c, (p, t, cost) in spec.items()})


def test_cheaper_failures_dont_count_as_savings():
    inc = _res({**{f"ok{i}": (2, 2, 0.20) for i in range(6)}, "f": (0, 2, 2.00)})
    cand = _res({**{f"ok{i}": (2, 2, 0.20) for i in range(6)}, "f": (0, 2, 0.02)})   # failure got cheap
    _, dC_all, _, _ = paired_deltas(inc, cand)
    _, dC_shared, _, _ = paired_deltas(inc, cand, AcceptanceConfig(cost_on_shared_successes=True))
    assert dC_all < -0.5 and dC_shared == 0.0


def test_too_few_shared_successes_gives_no_cost_credit():
    inc = _res({"a": (2, 2, 1.0), "b": (0, 2, 1.0)})
    cand = _res({"a": (2, 2, 0.1), "b": (0, 2, 1.0)})
    _, dC, _, _ = paired_deltas(inc, cand, AcceptanceConfig(cost_on_shared_successes=True, min_shared_successes=5))
    assert dC == 0.0


def test_in_band_positive_candidate_is_a_near_miss():
    inc = _res({f"c{i}": (1, 2, 0.1) for i in range(10)})
    cand = _res({**{f"c{i}": (1, 2, 0.1) for i in range(9)}, "c9": (2, 2, 0.1)})       # dS = +0.05
    d = decide(inc, cand, S_star=0.5, cfg=AcceptanceConfig(delta=0.08, near_miss_fraction=0.5))
    assert not d.accept and d.near_miss
    d_off = decide(inc, cand, S_star=0.5, cfg=AcceptanceConfig(delta=0.08))
    assert not d_off.near_miss
