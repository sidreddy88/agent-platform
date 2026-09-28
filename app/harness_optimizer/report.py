"""
The held-out report: does the evolved harness beat the original, and does it
beat simply rerunning the original?

Both harnesses run on the held-out cases (repos the optimizer never saw) with
the same number of trials, k >= 2. From those trials:

- pass@1 of each harness: the single-run localization rate.
- The matched-budget rerun baseline (AI2, 2607.12227): the original given two
  attempts, i.e. its pass@2, estimated without bias from its own k trials
  (app/evals/pass_k.py). An evolved harness that costs one run should beat
  that, or you could have just rerun. No separate rerun arm is needed.
- pass^k: reliability, the chance all k attempts pass.
- A paired bootstrap over cases for each difference, since both harnesses ran
  on the same cases: a 95% interval, not just a point estimate.
"""
from __future__ import annotations

import json
import random
from pathlib import Path

from app.evals.pass_k import pass_at_k, pass_hat_k

from app.harness_optimizer.acceptance import CaseResult


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _bootstrap(diffs: list[float], n: int = 5000, seed: int = 0) -> tuple[float, float]:
    rng = random.Random(seed)
    means = sorted(_mean([rng.choice(diffs) for _ in diffs]) for _ in range(n))
    return means[int(0.025 * n)], means[int(0.975 * n) - 1]


def _arm(per_case: dict[str, CaseResult], k: int) -> dict:
    trials = sum(r.trials for r in per_case.values())
    out = {
        "cases": len(per_case),
        "trials_per_case": k,
        "cost_per_trial_usd": sum(r.cost_usd for r in per_case.values()) / trials if trials else 0.0,
        "escalation_rate": sum(r.escalations for r in per_case.values()) / trials if trials else 0.0,
    }
    for j in range(1, k + 1):
        out[f"pass@{j}"] = _mean([pass_at_k(r.trials, r.passes, j) for r in per_case.values()])
        out[f"pass^{j}"] = _mean([pass_hat_k(r.trials, r.passes, j) for r in per_case.values()])
    return out


def build(original: dict[str, CaseResult], evolved: dict[str, CaseResult] | None, k: int,
          meta: dict) -> dict:
    report = {"meta": meta, "original": _arm(original, k)}
    if evolved is None:
        report["evolved"] = None
        report["verdict"] = ("No edit was accepted, so the evolved harness is the original: "
                             "nothing to compare. The held-out numbers are the original's.")
        return report
    cases = sorted(set(original) & set(evolved))
    report["evolved"] = _arm({c: evolved[c] for c in cases}, k)
    comparisons = {}
    for name, orig_k in (("vs_original_single_run", 1), ("vs_rerun_baseline_best_of_2", 2)):
        diffs = [pass_at_k(evolved[c].trials, evolved[c].passes, 1)
                 - pass_at_k(original[c].trials, original[c].passes, orig_k) for c in cases]
        lo, hi = _bootstrap(diffs)
        comparisons[name] = {"diff": _mean(diffs), "ci95": [lo, hi],
                             "clear": lo > 0 or hi < 0,
                             "direction": "better" if lo > 0 else "worse" if hi < 0 else "within noise"}
    improved = [c for c in cases if evolved[c].score > original[c].score]
    regressed = [c for c in cases if evolved[c].score < original[c].score]
    report["comparisons"] = comparisons
    report["paired"] = {"improved": improved, "regressed": regressed,
                        "unchanged": len(cases) - len(improved) - len(regressed)}
    b = comparisons["vs_rerun_baseline_best_of_2"]
    report["verdict"] = (
        f"Evolved pass@1 vs original best-of-2 (matched budget): {b['diff']:+.3f} "
        f"(95% CI {b['ci95'][0]:+.3f} to {b['ci95'][1]:+.3f}), {b['direction']}.")
    return report


def write(out_dir: Path, report: dict) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=1))
    lines = ["# Held-out report", "", report["verdict"], ""]
    arms = [("original", report["original"])] + ([("evolved", report["evolved"])] if report.get("evolved") else [])
    keys = [k for k in arms[0][1] if k.startswith(("pass", "cost", "escalation"))]
    lines.append("| | " + " | ".join(name for name, _ in arms) + " |")
    lines.append("|---|" + "---|" * len(arms))
    for key in keys:
        lines.append(f"| {key} | " + " | ".join(f"{a[key]:.3f}" for _, a in arms) + " |")
    for name, c in (report.get("comparisons") or {}).items():
        lines.append(f"\n**{name}:** {c['diff']:+.3f} (95% CI {c['ci95'][0]:+.3f} to "
                     f"{c['ci95'][1]:+.3f}), {c['direction']}")
    if report.get("paired"):
        p = report["paired"]
        lines.append(f"\nPer case: {len(p['improved'])} improved, {len(p['regressed'])} regressed, "
                     f"{p['unchanged']} unchanged.")
    lines.append("\n```\n" + json.dumps(report["meta"], indent=1) + "\n```\n")
    (out_dir / "report.md").write_text("\n".join(lines))
