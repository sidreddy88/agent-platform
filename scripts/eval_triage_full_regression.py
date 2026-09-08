"""
Hard regression gate for TriageAgent: replay every case in the 108-case
held-out set (app/evals/triage_heldout.jsonl -- stratified, never used in
training/fine-tuning, see scripts/split_triage_dataset.py) and require every
single one to still PASS. Exit non-zero if even one regresses.

This is the enforcement mechanism behind CI's triage-regression workflow
(.github/workflows/triage-regression.yml) -- any PR touching
app/agents/triage.py or app/agents/base.py must pass this in full before
merging.

Why PASS only, not PASS+DRIFT: mirrors scripts/eval_diagnosis_full_regression.py's
same design choice. scripts/eval_triage_regression.py treats a one-level
severity DRIFT as non-fatal (tracking a boundary shift for a human to
review), but a hard "only allow the change if everything passes" gate should
require the literal PASS verdict.

Accepted tradeoff, stated plainly: TriageAgent's prompt has no explicit
temperature=0 pin (see app/agents/triage.py), so a small amount of
sample-to-sample variance on genuinely close severity-boundary cases is
possible even with zero real regression. DiagnosisAgent's full-regression
gate accepts the same class of risk for the same reason (see its docstring)
-- if this gate turns out to be flaky in practice, the fix is pinning
temperature on the triage call, not loosening this gate's pass bar.

Much cheaper and faster than DiagnosisAgent's equivalent: no git checkout,
no GitHub API calls, no target-repo cloning -- every fact TriageAgent needs
is replayed through the same controllable mock stubs used to build the
dataset (scripts/generate_triage_synthetic_dataset.py). One real Haiku call
per case, nothing else. Expect low single-digit minutes for all 108 cases,
not the hour-plus DiagnosisAgent's Sonnet-plus-tool-loop gate needs.

Usage:
    python scripts/eval_triage_full_regression.py

Or let CI run it automatically on a PR touching TriageAgent.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.eval_triage_regression import _load_cases, _replay_one

_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_heldout.jsonl"


async def _run_suite() -> list[dict[str, Any]]:
    cases = _load_cases(_DATASET)
    if not cases:
        print(f"WARNING: no held-out cases found at {_DATASET}.")
        return []
    results = []
    for i, case in enumerate(cases, 1):
        label = case.get("input", {}).get("title", case.get("id"))
        print(f"[{i}/{len(cases)}] {label} ...", flush=True)
        try:
            result = await _replay_one(case)
        except Exception as exc:
            result = {"id": case.get("id"), "title": label, "verdict": "ERROR",
                      "detail": f"replay raised: {exc}"}
        print(f"    -> {result['verdict']}: {result['detail']}", flush=True)
        results.append(result)
    return results


def _print_report(results: list[dict[str, Any]]) -> None:
    total = len(results)
    regressions = [r for r in results if r["verdict"] != "PASS"]
    print(f"\n# TriageAgent full regression gate (N={total})\n")
    print(f"PASS: {total - len(regressions)}  NOT-PASS: {len(regressions)}\n")
    if regressions:
        print("Regressions (blocking):")
        for r in regressions:
            print(f"  {r['verdict']} — {r.get('id')}: {r['detail']}")
    else:
        print("No regressions. Every known-good case still passes.")


async def _main() -> int:
    results = await _run_suite()
    _print_report(results)
    has_regression = any(r["verdict"] != "PASS" for r in results)
    return 1 if has_regression else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
