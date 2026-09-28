"""
Diagnosis regression gate v2: a paired statistical test over the whole
non-held-out benchmark, instead of a failure count on cases that always pass.

Why not the v1 shape (56 cases the model always passes, fail if more than 6
don't): a regression lowers every case's chance of passing, and a case that
passes 97% of the time barely moves. Simulated on the 500-case DeepSeek run,
100 always-pass cases with a fixed threshold caught a 3.8-point drop in
overall localization 33% of the time; all 434 cases compared one by one
against main's own measured rate caught a 3.5-point drop 84% of the time
(2 trials per case), with fewer false alarms. The cases that sometimes pass
are where a regression shows first, and they're only usable when each one is
compared with what main does on that same case.

The test. For each case i: the PR's pass rate a_i/k and main's b_i/m (from the
stored baseline). Under "nothing changed", both come from the same per-case
rate, estimated by pooling: p_i = (a_i + b_i) / (k + m). Then

    T = sum_i (a_i/k - b_i/m)          V = sum_i p_i (1 - p_i) (1/k + 1/m)
    z = T / sqrt(V)                    fail if z < -z_alpha  (one-sided)

Cases both sides always pass contribute nothing to T or V; a PR that breaks
one does (p_i drops below 1). The gate fails closed: a pool case missing from
the PR's results or the baseline is a failure, never a skip.

Also here: the case pool, sharding, the baseline format, and the power
analysis that sizes the gate from the baseline's own per-case rates.
"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
FULL = ROOT / "app" / "evals" / "swebench_verified_full.jsonl"
SPLIT = ROOT / "app" / "evals" / "harness_split.json"
POOL = ROOT / "app" / "evals" / "gate_v2_cases.json"
BASELINE = ROOT / "app" / "evals" / "gate_v2_baseline.json"

Z_ALPHA = 1.645        # one-sided, alpha = 0.05


# ---- the case pool -----------------------------------------------------------

def build_pool(full: Path = FULL, split: Path = SPLIT) -> dict:
    """Every SWE-bench Verified case outside the held-out repos, sorted by repo.
    The held-out repos stay out: the gate decides which harness changes merge,
    so it must never touch the cases kept aside for measuring the optimizer."""
    heldout = set(json.loads(split.read_text())["heldout_repos"])
    rows = [json.loads(line) for line in full.read_text().splitlines() if line.strip()]
    cases = sorted(((r["repo"], r["instance_id"]) for r in rows if r["repo"] not in heldout))
    return {
        "_doc": "Gate v2 case pool: SWE-bench Verified minus the held-out repos. "
                "Built by scripts/gate_v2.py pool; see app/evals/gate_v2.py.",
        "excluded_repos": sorted(heldout),
        "cases": [cid for _, cid in cases],
        "repos": {cid: repo for repo, cid in cases},
    }


def load_pool(path: Path = POOL) -> dict:
    return json.loads(path.read_text())


# Relative time per case, measured by the CI probe (run 36455388528, 2 trials,
# 3 concurrent replays per shard): astropy 45 s and django 45 s per replay,
# sympy 71 s. Repos not measured count as 1.0.
REPO_WEIGHTS = {"sympy/sympy": 1.6}


def shard(cases: list[str], of: int, index: int, repos: dict[str, str] | None = None,
          weights: dict[str, float] | None = None) -> list[str]:
    """Contiguous slices of the repo-sorted pool (index is 1-based), cut so each
    slice has about the same total expected time. A full run is only as fast as
    its slowest shard, and a sympy case takes ~1.6x a django one, so equal case
    counts left sympy's shards ~20 minutes behind. Contiguous rather than
    round-robin: each shard then clones one or two repos, not all of them."""
    if not 1 <= index <= of:
        raise ValueError(f"shard {index}/{of}")
    w = [(weights or {}).get((repos or {}).get(c, ""), 1.0) for c in cases]
    total = sum(w)
    # Shard k takes the cases whose cumulative-weight midpoint falls in its
    # 1/of of the total: every case lands in exactly one shard.
    out, acc = [], 0.0
    for c, wi in zip(cases, w):
        mid = acc + wi / 2
        if (index - 1) * total / of <= mid < index * total / of or (index == of and mid >= total):
            out.append(c)
        acc += wi
    return out


# ---- results and the baseline ------------------------------------------------

Verdicts = dict[str, list[str]]         # case id -> one verdict per trial


def collect(run_dirs: list[Path], harness_hash: str, trials: int) -> Verdicts:
    """Per-case verdicts from optimizer run dirs (evals/<hash>/k<trials>/<case>.json)."""
    out: Verdicts = {}
    for rd in run_dirs:
        for p in sorted((Path(rd) / "evals" / harness_hash / f"k{trials}").glob("*.json")):
            out[p.stem] = list(json.loads(p.read_text())["case"]["verdicts"])
    return out


def merge(*parts: Verdicts) -> Verdicts:
    """Combine trials of the same harness measured in separate runs."""
    out: Verdicts = {}
    for part in parts:
        for cid, v in part.items():
            out.setdefault(cid, []).extend(v)
    return out


def load_baseline(path: Path = BASELINE) -> dict:
    return json.loads(path.read_text())


def _passes(verdicts: list[str]) -> tuple[int, int]:
    return sum(v == "PASS" for v in verdicts), len(verdicts)


# ---- the test -----------------------------------------------------------------

@dataclass
class GateResult:
    passed: bool
    reason: str
    cases: int = 0
    z: float = 0.0
    mean_diff: float = 0.0              # PR pass rate minus main's, averaged over cases
    pr_rate: float = 0.0
    base_rate: float = 0.0
    missing: list[str] = field(default_factory=list)
    regressed: list[str] = field(default_factory=list)   # main mostly passes, PR never did
    improved: list[str] = field(default_factory=list)    # main mostly fails, PR always did
    by_repo: dict[str, dict] = field(default_factory=dict)


def paired_test(pr: Verdicts, base: Verdicts, pool: list[str],
                repos: dict[str, str] | None = None, z_alpha: float = Z_ALPHA) -> GateResult:
    """z_alpha: the failure threshold is z < -z_alpha. The normal default (1.645)
    runs hot on this data (many near-100% cases, few trials each: 8% false alarms
    modelled on the 500-case run, not 5%), so the gate uses the threshold stored
    in the baseline by calibrate_threshold instead."""
    missing = [c for c in pool if not pr.get(c) or not base.get(c)]
    if missing:
        return GateResult(False, f"{len(missing)} pool case(s) missing from the PR's results or "
                                 f"the baseline: failing closed", cases=len(pool), missing=missing)
    t = v = 0.0
    pr_sum = base_sum = 0.0
    regressed, improved = [], []
    by_repo: dict[str, dict] = {}
    for c in pool:
        a, k = _passes(pr[c])
        b, m = _passes(base[c])
        d = a / k - b / m
        p = (a + b) / (k + m)
        var = p * (1 - p) * (1 / k + 1 / m)
        t += d
        v += var
        pr_sum += a / k
        base_sum += b / m
        if b / m >= 0.75 and a == 0:
            regressed.append(c)
        if b / m <= 0.25 and a == k:
            improved.append(c)
        r = by_repo.setdefault((repos or {}).get(c, c.split("__")[0]), {"cases": 0, "t": 0.0, "v": 0.0})
        r["cases"] += 1
        r["t"] += d
        r["v"] += var
    n = len(pool)
    z = t / math.sqrt(v) if v > 0 else (0.0 if t == 0 else -math.inf)
    for r in by_repo.values():
        r["mean_diff"] = r["t"] / r["cases"]
        r["z"] = r["t"] / math.sqrt(r["v"]) if r["v"] > 0 else 0.0
    passed = z >= -z_alpha
    reason = (f"z = {z:.2f} (fail below {-z_alpha:.3f}): localization "
              f"{'within noise of' if passed else 'significantly below'} main's baseline, "
              f"{t / n * 100:+.1f}pp over {n} cases")
    return GateResult(passed, reason, n, z, t / n, pr_sum / n, base_sum / n,
                      regressed=regressed, improved=improved, by_repo=by_repo)


def report(res: GateResult) -> str:
    lines = [f"## Diagnosis gate v2: {'PASS' if res.passed else 'FAIL'}", "", res.reason, ""]
    if res.missing:
        lines += [f"Missing ({len(res.missing)}): " + ", ".join(res.missing[:20]), ""]
        return "\n".join(lines)
    lines += ["| | PR | main baseline |", "|---|---|---|",
              f"| Mean per-case pass rate | {res.pr_rate:.1%} | {res.base_rate:.1%} |", ""]
    lines += ["| Repo | Cases | Diff | z |", "|---|---|---|---|"]
    for repo, r in sorted(res.by_repo.items(), key=lambda kv: kv[1]["z"]):
        lines.append(f"| {repo} | {r['cases']} | {r['mean_diff'] * 100:+.1f}pp | {r['z']:.2f} |")
    lines.append("")
    if res.regressed:
        lines.append(f"Regressed (main passes ≥75%, PR never): {', '.join(res.regressed)}")
    if res.improved:
        lines.append(f"Improved (main passes ≤25%, PR always): {', '.join(res.improved)}")
    return "\n".join(lines)


# ---- sizing the gate from the baseline ---------------------------------------

def false_alarm_rate(base: Verdicts, pool: list[str], pr_trials: int, sims: int = 2000,
                     seed: int = 0, z_alpha: float = Z_ALPHA) -> float:
    """Measured, not modelled: split each case's baseline trials into a pretend
    PR (pr_trials of them) and a pretend baseline (the rest), many times, and
    count how often the test fails when nothing changed. Needs more baseline
    trials per case than pr_trials."""
    rng = random.Random(seed)
    fails = 0
    for _ in range(sims):
        pr, bs = {}, {}
        for c in pool:
            v = base[c][:]
            rng.shuffle(v)
            pr[c], bs[c] = v[:pr_trials], v[pr_trials:]
        fails += not paired_test(pr, bs, pool, z_alpha=z_alpha).passed
    return fails / sims


def _simulate(base: Verdicts, pool: list[str], pr_trials: int, shift: float,
              rng: random.Random) -> tuple[Verdicts, Verdicts, float]:
    pr: Verdicts = {}
    bs: Verdicts = {}
    d = 0.0
    for c in pool:
        b, m = _passes(base[c])
        p = min(max(rng.betavariate(b + 0.5, m - b + 0.5), 1e-6), 1 - 1e-6)
        q = 1 / (1 + math.exp(-(math.log(p / (1 - p)) - shift)))
        d += p - q
        bs[c] = ["PASS" if rng.random() < p else "FAIL" for _ in range(m)]
        pr[c] = ["PASS" if rng.random() < q else "FAIL" for _ in range(pr_trials)]
    return pr, bs, d / len(pool)


def calibrate_threshold(base: Verdicts, pool: list[str], pr_trials: int, alpha: float = 0.05,
                        sims: int = 2000, seed: int = 1) -> float:
    """The z_alpha that gives an `alpha` false-alarm rate on this baseline:
    simulate no-change runs (both sides drawn from each case's own rate, as in
    power), take the alpha-quantile of z. A parametric bootstrap, so the gate's
    threshold fits the data's real discreteness instead of the normal curve."""
    rng = random.Random(seed)
    zs = sorted(paired_test(*_simulate(base, pool, pr_trials, 0.0, rng)[:2], pool).z
                for _ in range(sims))
    return -zs[int(alpha * sims)]


def power(base: Verdicts, pool: list[str], pr_trials: int, shift: float,
          sims: int = 1000, seed: int = 0, z_alpha: float = Z_ALPHA) -> tuple[float, float]:
    """Modelled: how often the gate catches a regression that lowers every
    case's log-odds of passing by `shift`. Each simulation draws each case's
    true rate from its baseline trials (Jeffreys prior, so sometimes-pass
    cases carry their real uncertainty), then simulates BOTH sides from it: a
    fresh baseline with the same number of trials at the true rate, and the PR
    at the shifted rate. (Comparing simulated PRs with the observed baseline
    instead is biased: a case that passed 2/2 has a true rate below 1 on
    average, so even shift 0 looks like a regression.) shift 0 gives the
    modelled false-alarm rate. Returns (detection rate, mean drop in pass rate)."""
    rng = random.Random(seed)
    caught, drop = 0, 0.0
    for _ in range(sims):
        pr, bs, d = _simulate(base, pool, pr_trials, shift, rng)
        drop += d
        caught += not paired_test(pr, bs, pool, z_alpha=z_alpha).passed
    return caught / sims, drop / sims
