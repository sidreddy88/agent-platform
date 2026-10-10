#!/usr/bin/env python
"""
Test-mode A/B: grade two fix runs on the same cases and compare them.

Both arms are ordinary eval_swebench_fix.py runs on app/evals/fix_test_mode_ab.json
(same saved diagnoses and flags); the treatment uses a harness copy with
test_mode = existing_tests. This script grades each arm with the official
SWE-bench harness on Modal and reports, per group (broke_tests / random):
resolved, cases won and lost (paired sign test), newly broken passing cases,
cost per case, and median / p90 seconds per case.

    python scripts/ab_test_mode.py --control ab-control --treatment ab-test-mode
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.harness_optimizer.fix_evaluator import modal_grade  # noqa: E402

RUNS = ROOT / "runs" / "fix"
CASES = ROOT / "app" / "evals" / "fix_test_mode_ab.json"


def load_run(name: str) -> tuple[dict[str, dict], list[dict]]:
    d = RUNS / name
    recs = {r["instance_id"]: r for r in map(json.loads, (d / "results.jsonl").read_text().splitlines()) if r}
    preds = [json.loads(x) for x in (d / "predictions.jsonl").read_text().splitlines() if x.strip()] \
        if (d / "predictions.jsonl").exists() else []
    return recs, preds


def graded(name: str, preds: list[dict]) -> dict[str, bool]:
    cache = RUNS / name / "resolved.json"
    if cache.exists():
        return json.loads(cache.read_text())
    out = modal_grade(preds, run_id=f"{name}-grade", workdir=RUNS / name / "grading")
    cache.write_text(json.dumps(out, indent=1))
    return out


def sign_test_p(wins: int, losses: int) -> float:
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def summarize(group: str, ids: list[str], ctl: dict, trt: dict, res_c: dict, res_t: dict) -> dict:
    rc = {i: bool(res_c.get(i)) for i in ids}
    rt_ = {i: bool(res_t.get(i)) for i in ids}
    wins = [i for i in ids if rt_[i] and not rc[i]]
    losses = [i for i in ids if rc[i] and not rt_[i]]

    def fx(recs, i, key):
        return ((recs.get(i) or {}).get("fix") or {}).get(key)

    def costs(recs):
        return [fx(recs, i, "cost_usd") or 0.0 for i in ids]

    def secs(recs):
        return [s for s in (fx(recs, i, "seconds") for i in ids) if s is not None]

    retried = [i for i in ids if any((r.get("run") or "").startswith("attempt 2")
                                    for r in (fx(trt, i, "test_runs") or []))]
    tested = [i for i in ids if any((r.get("run") or "") == "before" and not r.get("infra_error")
                                   for r in (fx(trt, i, "test_runs") or []))]
    return {
        "group": group, "cases": len(ids),
        "resolved_control": sum(rc.values()), "resolved_test_mode": sum(rt_.values()),
        "won": wins, "lost": losses, "sign_test_p": round(sign_test_p(len(wins), len(losses)), 4),
        "cost_per_case_control": round(statistics.mean(costs(ctl)), 4) if ids else 0,
        "cost_per_case_test_mode": round(statistics.mean(costs(trt)), 4) if ids else 0,
        "median_s_control": round(statistics.median(secs(ctl)), 1) if secs(ctl) else None,
        "median_s_test_mode": round(statistics.median(secs(trt)), 1) if secs(trt) else None,
        "p90_s_control": round(pct(secs(ctl), 0.9), 1), "p90_s_test_mode": round(pct(secs(trt), 0.9), 1),
        "tests_ran": len(tested), "retried": len(retried),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", required=True)
    ap.add_argument("--treatment", required=True)
    args = ap.parse_args()
    cases = json.loads(CASES.read_text())
    ctl, preds_c = load_run(args.control)
    trt, preds_t = load_run(args.treatment)
    res_c, res_t = graded(args.control, preds_c), graded(args.treatment, preds_t)
    report = [summarize(g, cases[g], ctl, trt, res_c, res_t) for g in ("broke_tests", "random")]
    report.append(summarize("all", cases["broke_tests"] + cases["random"], ctl, trt, res_c, res_t))
    out = RUNS / args.treatment / "ab_report.json"
    out.write_text(json.dumps(report, indent=1))
    for r in report:
        print(json.dumps(r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
