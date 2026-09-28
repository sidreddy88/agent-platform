"""Gate v2's paired test: passes when nothing changed, fails on a real drop,
fails closed on missing cases, and its pool never touches the held-out repos."""
from __future__ import annotations

import json

from app.evals import gate_v2

P, F = "PASS", "FAIL"


def _pool(n=200):
    return [f"repo{i % 4}__case-{i}" for i in range(n)]


def _mixed(pool, pr_trials=2):
    """Main's baseline: most cases always pass, some sometimes, a few never."""
    base, pr = {}, {}
    for i, c in enumerate(pool):
        kind = i % 10
        base[c] = [P] * 4 if kind < 7 else [P, F, P, F] if kind < 9 else [F] * 4
        pr[c] = base[c][:pr_trials]
    return base, pr


def test_identical_results_pass():
    pool = _pool()
    base, pr = _mixed(pool)
    res = gate_v2.paired_test(pr, base, pool)
    assert res.passed and res.missing == []


def test_clear_regression_fails():
    pool = _pool()
    base, pr = _mixed(pool)
    for c in pool[::5]:                      # every 5th case now fails both trials
        pr[c] = [F, F]
    res = gate_v2.paired_test(pr, base, pool)
    assert not res.passed and res.z < -gate_v2.Z_ALPHA
    assert res.regressed                     # always-pass cases that now never pass are listed


def test_improvement_never_fails_the_gate():
    pool = _pool()
    base, pr = _mixed(pool)
    for c in pool:
        pr[c] = [P, P]
    res = gate_v2.paired_test(pr, base, pool)
    assert res.passed and res.z > 0 and res.improved


def test_breaking_one_always_pass_case_counts():
    pool = [f"r__c{i}" for i in range(10)]
    base = {c: [P] * 4 for c in pool}
    pr = {c: [P, P] for c in pool}
    pr["r__c0"] = [F, F]
    res = gate_v2.paired_test(pr, base, pool)
    assert res.mean_diff < 0 and res.z < 0


def test_missing_case_fails_closed():
    pool = _pool(20)
    base, pr = _mixed(pool)
    del pr[pool[3]]
    res = gate_v2.paired_test(pr, base, pool)
    assert not res.passed and res.missing == [pool[3]]


def test_shards_cover_the_pool_exactly_once():
    pool = _pool(434)
    parts = [gate_v2.shard(pool, 20, i) for i in range(1, 21)]
    assert sum(parts, []) == pool
    assert max(map(len, parts)) - min(map(len, parts)) <= 1


def test_false_alarm_rate_is_near_alpha_when_nothing_changes():
    pool = _pool(300)
    base, _ = _mixed(pool)
    rate = gate_v2.false_alarm_rate(base, pool, pr_trials=2, sims=300)
    assert rate <= 0.12


def test_modelled_false_alarms_near_alpha_at_zero_shift():
    pool = _pool(300)
    base, _ = _mixed(pool)
    rate, drop = gate_v2.power(base, pool, 2, shift=0.0, sims=300)
    assert rate <= 0.12 and abs(drop) < 1e-9


def test_power_grows_with_the_regression():
    pool = _pool(300)
    base, _ = _mixed(pool)
    small, _ = gate_v2.power(base, pool, 2, shift=0.3, sims=150)
    large, drop = gate_v2.power(base, pool, 2, shift=1.5, sims=150)
    assert large > small and large > 0.8 and drop > 0


def test_pool_excludes_heldout_repos():
    pool = json.loads(gate_v2.POOL.read_text())
    heldout = set(json.loads(gate_v2.SPLIT.read_text())["heldout_repos"])
    assert not {pool["repos"][c] for c in pool["cases"]} & heldout
    assert len(pool["cases"]) == 434


def test_calibrated_threshold_holds_false_alarms_at_alpha():
    pool = _pool(300)
    base, _ = _mixed(pool)
    z = gate_v2.calibrate_threshold(base, pool, 2, sims=400)
    rate, _ = gate_v2.power(base, pool, 2, shift=0.0, sims=400, seed=99, z_alpha=z)
    assert 0.01 <= rate <= 0.10


def test_weighted_shards_cover_the_pool_and_balance_time():
    pool = json.loads(gate_v2.POOL.read_text())
    cases, repos = pool["cases"], pool["repos"]
    parts = [gate_v2.shard(cases, 20, i, repos, gate_v2.REPO_WEIGHTS) for i in range(1, 21)]
    assert sum(parts, []) == cases
    cost = [sum(gate_v2.REPO_WEIGHTS.get(repos[c], 1.0) for c in p) for p in parts]
    assert max(cost) - min(cost) <= 1.6          # within one sympy case
