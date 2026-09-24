"""
Regression gate for DiagnosisAgent: replay every case in the golden dataset
(6 real AllInterviews production incidents + 56 SWE-bench Verified instances
DiagnosisAgent is known to get right) and fail if more than MAX_NONPASS_RATE
of them stop passing.

This is the enforcement mechanism behind CI's diagnosis-regression workflow
(.github/workflows/diagnosis-regression.yml) -- any PR touching
app/agents/diagnosis.py or app/agents/base.py must pass this before merging.

Why 56, not the full 100-instance SWE-bench sample: the other 44 already
fail today (see docs/blog-drafts/swebench-results-log.md for the full
per-instance breakdown) -- mixing known-failures into the gate would make it
permanently red and useless. This tracks regressions against a *known-good*
baseline, not "does DiagnosisAgent generalize" (that's
scripts/eval_swebench_diagnosis.py's job, run manually against the full
100-instance app/evals/swebench_verified_sample.jsonl, not part of this gate).

Why PASS is the only verdict that counts, not PASS+DRIFT: the existing
scripts/eval_diagnosis_regression.py treats DRIFT (same file, confidence
moved beyond tolerance) as non-fatal, since it's tracking behavior shift for
a human to review, not gating a merge. Here every non-PASS verdict counts
toward the threshold.

Why a threshold, not the original literal 100%: the 100% version never once
completed in CI. At ~7.5 min/case the sequential 62-case replay needs ~8h,
past both the job's 180-min timeout and GitHub's 6h hard limit, so every
real run since 2026-09-07 was cancelled -- and even if it had finished,
identical reruns flip cases (psf__requests-1142 went PASS -> FAIL with no
code change), so a zero-tolerance bar can't tell noise from a regression.
Same lesson scripts/eval_triage_full_regression.py already learned.

Runtime is fixed by sharding: CI runs this with --shard K/N across a job
matrix, each shard writes its verdicts with --results-out, and a final job
merges them with --aggregate and applies the threshold. The aggregate step
fails if any shard is missing or incomplete, so a crashed or cancelled
shard can never shrink the denominator into a pass.

Real Anthropic + GitHub API calls, real cost. Not part of the mocked pytest
suite. Run manually via:

    python scripts/eval_diagnosis_full_regression.py                 # everything, one process
    python scripts/eval_diagnosis_full_regression.py --shard 2/8 --results-out r/2.json
    python scripts/eval_diagnosis_full_regression.py --aggregate r/ --expect-shards 8

Or let CI run it automatically on a PR touching DiagnosisAgent.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_PRODUCTION_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "diagnosis_regression.jsonl"
_SWEBENCH_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "swebench_diagnosis_regression.jsonl"

# PROVISIONAL -- not yet calibrated. The only noise evidence so far is one
# known flip (psf__requests-1142) on an identical rerun; there is no measured
# noise floor for this gate yet, because it has never completed. Calibrate by
# running the gate on unchanged main at least twice (workflow_dispatch) and
# setting this comfortably above the observed non-pass rate, the way
# eval_triage_full_regression.py's 6% was set. At N=56, 10% allows 6 cases.
MAX_NONPASS_RATE = 0.10


def _select_shard(items: list[Any], shard: int, of: int, offset: int = 0) -> list[Any]:
    """Round-robin slice: item i belongs to shard ((offset + i) % of) + 1.

    Round-robin rather than contiguous blocks so each shard gets a mix of
    repos -- the dataset is grouped by repo, and some repos are much slower
    to clone and explore than others. `offset` lets the production and
    SWE-bench suites share one global numbering, so the six production cases
    spread across shards instead of all landing on shard 1.
    """
    return [item for i, item in enumerate(items) if (offset + i) % of == shard - 1]


def _load_production() -> list[dict[str, Any]]:
    from scripts.eval_diagnosis_regression import _load_cases

    cases = _load_cases(_PRODUCTION_DATASET)
    if not cases:
        print(f"WARNING: no production cases found at {_PRODUCTION_DATASET} "
              f"(gitignored -- real incident data, expected to be absent outside the "
              f"machine that built it). Skipping this half of the gate.")
    return cases


def _load_swebench() -> list[dict[str, Any]]:
    from scripts.eval_swebench_diagnosis import _load_instances

    instances = _load_instances(_SWEBENCH_DATASET)
    if not instances:
        print(f"WARNING: no SWE-bench baseline instances found at {_SWEBENCH_DATASET}.")
    return instances


async def _run_production_suite(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.services.github import GitHubService
    from scripts.eval_diagnosis_regression import _replay_one as _replay_production_case

    if not cases:
        return []
    github = GitHubService()
    results = []
    for i, case in enumerate(cases, 1):
        label = case.get("event", {}).get("title", case.get("incident_id"))
        print(f"[production {i}/{len(cases)}] {label} ...", flush=True)
        try:
            result = await _replay_production_case(case, github)
        except Exception as exc:
            result = {"incident_id": case.get("incident_id"), "title": label,
                       "verdict": "ERROR", "detail": f"replay raised: {exc}"}
        print(f"    -> {result['verdict']}: {result['detail']}", flush=True)
        results.append({**result, "suite": "production"})
    return results


async def _run_swebench_suite(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from app.services.github import GitHubService
    from scripts.eval_swebench_diagnosis import _replay_one as _replay_swebench_instance

    if not instances:
        return []
    github = GitHubService()
    results = []
    for i, instance in enumerate(instances, 1):
        print(f"[swebench {i}/{len(instances)}] {instance['instance_id']} ({instance['repo']}) ...", flush=True)
        try:
            result = await _replay_swebench_instance(instance, github)
        except Exception as exc:
            result = {"instance_id": instance.get("instance_id"), "repo": instance.get("repo"),
                       "verdict": "ERROR", "detail": f"replay raised: {exc}"}
        print(f"    -> {result['verdict']}: {result['detail']}", flush=True)
        results.append({**result, "suite": "swebench"})
    return results


def _print_report(results: list[dict[str, Any]]) -> bool:
    """Returns True if the gate should FAIL (non-pass rate exceeds the threshold)."""
    total = len(results)
    non_pass = [r for r in results if r["verdict"] != "PASS"]
    rate = len(non_pass) / total if total else 0.0
    max_allowed = max(1, round(total * MAX_NONPASS_RATE))

    print(f"\n# DiagnosisAgent regression gate (N={total})\n")
    print(f"PASS: {total - len(non_pass)}  NOT-PASS: {len(non_pass)}  "
          f"({rate:.1%}, threshold {MAX_NONPASS_RATE:.0%} / max {max_allowed} cases)\n")
    if non_pass:
        print("Non-passing cases (informational unless the count exceeds threshold):")
        for r in non_pass:
            label = r.get("incident_id") or r.get("instance_id")
            print(f"  [{r['suite']}] {r['verdict']} — {label}: {r['detail']}")

    if total == 0:
        print("\nGate FAILS: zero cases replayed -- a gate that ran nothing proves nothing.")
        return True
    exceeds = len(non_pass) > max_allowed
    if exceeds:
        print(f"\nGate FAILS: {len(non_pass)} non-passes exceeds the {max_allowed}-case "
              f"threshold -- treat this as a real regression, not ordinary noise.")
    else:
        print(f"\nGate PASSES: {len(non_pass)} non-passes is within the {max_allowed}-case "
              f"threshold for this sample size.")
    return exceeds


def _aggregate(results_dir: Path, expect_shards: int) -> int:
    """Merge per-shard result files and apply the threshold. Fails closed on
    any missing or incomplete shard rather than scoring a smaller N."""
    files = sorted(results_dir.glob("*.json"))
    shards = [json.loads(f.read_text()) for f in files]
    seen = sorted(s["shard"] for s in shards)
    problems = []
    if seen != list(range(1, expect_shards + 1)):
        problems.append(f"expected shards 1..{expect_shards}, got {seen}")
    for s in shards:
        if s["of"] != expect_shards:
            problems.append(f"shard {s['shard']} was run as 1 of {s['of']}, not {expect_shards}")
        if len(s["results"]) != s["expected"]:
            problems.append(f"shard {s['shard']} recorded {len(s['results'])} of "
                            f"{s['expected']} assigned cases")
    if problems:
        print("Gate FAILS: shard results are incomplete, refusing to score a partial run:")
        for p in problems:
            print(f"  - {p}")
        return 1

    results = [r for s in shards for r in s["results"]]
    return 1 if _print_report(results) else 0


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--shard", help="K/N: replay only the K-th of N round-robin shards")
    parser.add_argument("--results-out", type=Path,
                        help="write this run's verdicts as JSON (for --aggregate)")
    parser.add_argument("--aggregate", type=Path, metavar="DIR",
                        help="merge shard result files in DIR and apply the threshold; runs no cases")
    parser.add_argument("--expect-shards", type=int, default=1,
                        help="with --aggregate: number of shard files that must be present")
    args = parser.parse_args()

    if args.aggregate:
        return _aggregate(args.aggregate, args.expect_shards)

    shard, of = 1, 1
    if args.shard:
        shard, of = (int(x) for x in args.shard.split("/"))
        if not 1 <= shard <= of:
            parser.error(f"--shard {args.shard}: K must be between 1 and N")

    all_production = _load_production()
    production = _select_shard(all_production, shard, of)
    swebench = _select_shard(_load_swebench(), shard, of, offset=len(all_production))
    if of > 1:
        print(f"Shard {shard}/{of}: {len(production)} production + {len(swebench)} SWE-bench cases", flush=True)

    results = await _run_production_suite(production) + await _run_swebench_suite(swebench)

    if args.results_out:
        args.results_out.parent.mkdir(parents=True, exist_ok=True)
        args.results_out.write_text(json.dumps({
            "shard": shard, "of": of,
            "expected": len(production) + len(swebench),
            "results": results,
        }, indent=2))

    has_regression = _print_report(results)
    # A shard's own verdict is informational: the threshold only means
    # something over the whole set, so --aggregate decides pass/fail.
    if of > 1:
        return 0
    return 1 if has_regression else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
