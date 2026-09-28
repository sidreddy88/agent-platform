"""
pass@k and pass^k from repeated trials, with the unbiased estimators.

    pass@k  P(at least one of k fresh attempts succeeds): capability, what a
            best-of-k rerun baseline buys.
    pass^k  P(all k attempts succeed): reliability. For a production incident
            agent this is the one that matters; a case that passes only
            sometimes is a case you can't trust unattended. (tau-bench, Yao et
            al. 2024, introduced pass^k for agents for this reason.)

From n trials of a case with c successes, the unbiased estimates
(Chen et al. 2021 for pass@k; the same combinatorics for pass^k) are:

    pass@k = 1 - C(n - c, k) / C(n, k)
    pass^k =     C(c, k)     / C(n, k)

averaged over cases. Both need n >= k. With n = 2 per case (what round 0 of
the harness optimizer runs), pass@1, pass@2 and pass^2 are estimable; pass^3
needs a third trial.

Why it matters here: DiagnosisAgent's single-run pass rate hid ~20% of cases
flipping between identical runs (psf__requests-1142 went PASS -> FAIL with no
code change). pass^k puts a number on that.
"""
from __future__ import annotations

from math import comb


def pass_at_k(n: int, c: int, k: int) -> float:
    if n < k:
        raise ValueError(f"need n >= k trials (n={n}, k={k})")
    return 1.0 - comb(n - c, k) / comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    if n < k:
        raise ValueError(f"need n >= k trials (n={n}, k={k})")
    return comb(c, k) / comb(n, k)


def summarize(trials: dict[str, tuple[int, int]], ks: tuple[int, ...] = (1, 2, 3)) -> dict:
    """trials: case -> (n trials, c successes). Returns, for every k that all
    cases have enough trials for, mean pass@k and pass^k over cases."""
    if not trials:
        return {"cases": 0}
    min_n = min(n for n, _ in trials.values())
    out: dict = {"cases": len(trials), "min_trials": min_n}
    for k in ks:
        if k > min_n:
            out[f"pass^{k}"] = out[f"pass@{k}"] = None      # not estimable with these trials
            continue
        out[f"pass@{k}"] = sum(pass_at_k(n, c, k) for n, c in trials.values()) / len(trials)
        out[f"pass^{k}"] = sum(pass_hat_k(n, c, k) for n, c in trials.values()) / len(trials)
    # Cases that are neither always-pass nor always-fail: the flaky ones.
    out["flaky_cases"] = sorted(case for case, (n, c) in trials.items() if 0 < c < n)
    return out
