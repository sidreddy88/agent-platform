"""
Hard regression gate for DiagnosisAgent: replay every case in the golden
dataset (6 real AllInterviews production incidents + 56 SWE-bench Verified
instances DiagnosisAgent is known to get right today) and require every
single one to still PASS. Exit non-zero if even one regresses.

This is the enforcement mechanism behind CI's diagnosis-regression workflow
(.github/workflows/diagnosis-regression.yml) -- any PR touching
app/agents/diagnosis.py or app/agents/base.py must pass this in full before
merging.

Why 56, not the full 100-instance SWE-bench sample: the other 44 already
fail today (see docs/blog-drafts/swebench-results-log.md for the full
per-instance breakdown) -- mixing known-failures into a "must all pass" gate
would make the gate permanently red and useless. This tracks regressions
against a *known-good* baseline, not "does DiagnosisAgent generalize" (that's
scripts/eval_swebench_diagnosis.py's job, run manually against the full
100-instance app/evals/swebench_verified_sample.jsonl, not part of this gate).

Why PASS is the only acceptable verdict here, not PASS+DRIFT: the existing
scripts/eval_diagnosis_regression.py treats DRIFT (same file, confidence
moved beyond tolerance) as non-fatal, since it's tracking behavior shift for
a human to review, not gating a merge. This script is stricter on purpose --
a hard "only allow the change if all of them pass" gate should require the
literal PASS verdict, not "close enough."

Real Anthropic + GitHub API calls, real cost, real runtime (expect over an
hour for 62 cases even parallelized -- see the CI workflow's timeout). Not
part of the mocked pytest suite. Run manually via:

    python scripts/eval_diagnosis_full_regression.py

Or let CI run it automatically on a PR touching DiagnosisAgent.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.eval_diagnosis_regression import _load_cases as _load_production_cases
from scripts.eval_diagnosis_regression import _replay_one as _replay_production_case
from scripts.eval_swebench_diagnosis import _load_instances as _load_swebench_instances
from scripts.eval_swebench_diagnosis import _replay_one as _replay_swebench_instance

_PRODUCTION_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "diagnosis_regression.jsonl"
_SWEBENCH_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "swebench_diagnosis_regression.jsonl"


async def _run_production_suite() -> list[dict[str, Any]]:
    from app.services.github import GitHubService

    cases = _load_production_cases(_PRODUCTION_DATASET)
    if not cases:
        print(f"WARNING: no production cases found at {_PRODUCTION_DATASET} "
              f"(gitignored -- real incident data, expected to be absent outside the "
              f"machine that built it). Skipping this half of the gate.")
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


async def _run_swebench_suite() -> list[dict[str, Any]]:
    from app.services.github import GitHubService

    instances = _load_swebench_instances(_SWEBENCH_DATASET)
    if not instances:
        print(f"WARNING: no SWE-bench baseline instances found at {_SWEBENCH_DATASET}.")
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


def _print_report(results: list[dict[str, Any]]) -> None:
    total = len(results)
    regressions = [r for r in results if r["verdict"] != "PASS"]
    print(f"\n# DiagnosisAgent full regression gate (N={total})\n")
    print(f"PASS: {total - len(regressions)}  NOT-PASS: {len(regressions)}\n")
    if regressions:
        print("Regressions (blocking):")
        for r in regressions:
            label = r.get("incident_id") or r.get("instance_id")
            print(f"  [{r['suite']}] {r['verdict']} — {label}: {r['detail']}")
    else:
        print("No regressions. Every known-good case still passes.")


async def _main() -> int:
    production_results = await _run_production_suite()
    swebench_results = await _run_swebench_suite()
    all_results = production_results + swebench_results

    _print_report(all_results)

    has_regression = any(r["verdict"] != "PASS" for r in all_results)
    return 1 if has_regression else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
