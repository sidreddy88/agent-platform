"""
Replay every case in app/evals/triage_heldout.jsonl through the CURRENT
TriageAgent and check it still reaches the same decision + severity it
reached when the dataset was built.

Sibling to scripts/eval_diagnosis_regression.py, same PASS/DRIFT/FAIL shape,
but structurally simpler than DiagnosisAgent's version: TriageAgent's ground
truth here doesn't depend on a moving target-repo state (no git checkout, no
GitHub API, no historical pinning). Every fact TriageAgent needs
(occurrence_count, has_existing_pr) is already recorded directly on the case
and replayed through the same controllable mock stubs used to build the
dataset in the first place (scripts/generate_triage_synthetic_dataset.py) --
so this makes exactly one real network call per case: the Haiku triage()
call itself.

Real Anthropic API calls (Haiku), same "live test" convention as
eval_diagnosis_regression.py -- not part of the mocked pytest suite, since
LLM output isn't deterministic and this specifically tests real model
behavior. Run manually after changing triage.py, or via CI (see
scripts/eval_triage_full_regression.py for the hard CI gate).

Scoring, adapted from DiagnosisAgent's PASS/DRIFT/FAIL (same spirit, no
continuous confidence value to use as a tolerance here -- severity is
categorical, so "adjacent severity" plays the same role "confidence within
tolerance" plays for Diagnosis):
  PASS  - decision matches AND severity matches exactly
  DRIFT - decision matches, severity is one level off (P1<->P2, P2<->P3,
          etc.) -- not a hard fail, flags a real boundary shift worth a
          human look, same non-fatal treatment as Diagnosis's confidence
          drift
  FAIL  - decision differs, or severity is off by 2+ levels

Usage:
    python scripts/eval_triage_regression.py
    python scripts/eval_triage_regression.py --dataset /tmp/other.jsonl
    python scripts/eval_triage_regression.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.generate_triage_synthetic_dataset import _ControllableAWSStub, _ControllableStoreStub

_DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_heldout.jsonl"

_SEVERITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_NON_SEVERITY_DECISIONS = {"noise", "duplicate"}  # severity still applies, just not incident-blast-radius-driven


def _load_cases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    cases = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                cases.append(json.loads(line))
    return cases


async def _replay_one(case: dict[str, Any]) -> dict[str, Any]:
    from app.agents.triage import TriageAgent
    from app.models.events import ErrorEvent, EventSource

    inp = case["input"]
    event = ErrorEvent(
        source=EventSource(inp.get("source", "cloudwatch")),
        error_type=inp.get("error_type"),
        title=inp.get("title", ""),
        description=inp.get("description", ""),
        service=inp.get("service", ""),
        metadata={"log_group": inp.get("log_group", "ecs/ContainerProcess/synthetic"),
                  "pattern": inp.get("error_type")},
    )

    aws_stub = _ControllableAWSStub(occurrence_count=inp["occurrence_count"])
    duplicate_pr_url = "https://github.com/example/repo/pull/1" if inp.get("has_existing_pr") else None
    store_stub = _ControllableStoreStub(duplicate_pr_url=duplicate_pr_url)

    agent = TriageAgent(aws=aws_stub, store=store_stub)
    result = await agent.triage(event)

    truth = case["output"]
    verdict = _score(truth, result)

    return {
        "id": case["id"],
        "title": inp.get("title", ""),
        "verdict": verdict["status"],
        "detail": verdict["detail"],
        "ground_truth_decision": truth["decision"],
        "ground_truth_severity": truth["severity"],
        "current_decision": result.decision,
        "current_severity": result.severity,
    }


def _score(truth: dict[str, Any], result: Any) -> dict[str, str]:
    if result.decision != truth["decision"]:
        return {
            "status": "FAIL",
            "detail": f"decision changed: {truth['decision']!r} -> {result.decision!r}",
        }

    truth_sev = truth["severity"]
    current_sev = result.severity
    if current_sev == truth_sev:
        return {"status": "PASS", "detail": "matches ground truth"}

    truth_rank = _SEVERITY_ORDER.get(truth_sev)
    current_rank = _SEVERITY_ORDER.get(current_sev)
    if truth_rank is None or current_rank is None:
        return {"status": "FAIL", "detail": f"unrecognized severity: {current_sev!r}"}

    if abs(truth_rank - current_rank) == 1:
        return {
            "status": "DRIFT",
            "detail": f"same decision, severity moved one level: {truth_sev} -> {current_sev}",
        }
    return {
        "status": "FAIL",
        "detail": f"same decision, severity moved {abs(truth_rank - current_rank)} levels: {truth_sev} -> {current_sev}",
    }


async def _run(cases: list[dict[str, Any]], verbose: bool = True) -> list[dict[str, Any]]:
    results = []
    for i, case in enumerate(cases, 1):
        if verbose:
            print(f"[{i}/{len(cases)}] {case.get('id')} — {case.get('input', {}).get('title', '')} ...", flush=True)
        try:
            result = await _replay_one(case)
        except Exception as exc:
            result = {
                "id": case.get("id"),
                "title": case.get("input", {}).get("title", ""),
                "verdict": "ERROR",
                "detail": f"replay raised: {exc}",
                "ground_truth_decision": case.get("output", {}).get("decision"),
                "ground_truth_severity": case.get("output", {}).get("severity"),
                "current_decision": None,
                "current_severity": None,
            }
        if verbose:
            print(f"    -> {result['verdict']}: {result['detail']}", flush=True)
        results.append(result)
    return results


def _print_report(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No regression cases found. Run scripts/split_triage_dataset.py first.")
        return

    counts = {"PASS": 0, "DRIFT": 0, "FAIL": 0, "ERROR": 0}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    print(f"# Triage regression check (N={len(results)})\n")
    print(f"PASS: {counts['PASS']}  DRIFT: {counts['DRIFT']}  FAIL: {counts['FAIL']}  ERROR: {counts['ERROR']}\n")

    for r in results:
        if r["verdict"] == "PASS":
            continue
        print(f"[{r['verdict']}] {r['id']} — {r['title']}")
        print(f"    {r['detail']}")

    if counts["FAIL"] == 0 and counts["ERROR"] == 0:
        print("\nNo regressions found." + (" Some severity drift to review." if counts["DRIFT"] else ""))
    else:
        print(f"\n{counts['FAIL'] + counts['ERROR']} regression(s) found — see above.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(_DEFAULT_DATASET), help="Path to the regression JSONL")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a report")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N cases (debugging)")
    args = parser.parse_args()

    cases = _load_cases(Path(args.dataset))
    if args.limit:
        cases = cases[:args.limit]
    results = asyncio.run(_run(cases, verbose=not args.json))

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_report(results)

    has_regression = any(r["verdict"] in ("FAIL", "ERROR") for r in results)
    return 1 if has_regression else 0


if __name__ == "__main__":
    sys.exit(main())
