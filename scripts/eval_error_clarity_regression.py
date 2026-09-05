"""
Replay every case in app/evals/error_clarity_regression.jsonl through the
CURRENT ErrorClarityAgent and check it still points at the same file(s) the
real merged observability PR touched.

Sibling to scripts/eval_diagnosis_regression.py, but a genuinely different
agent with a genuinely different notion of "correct" -- don't run these
cases through the diagnosis eval or vice versa. ErrorClarityAgent's job is
adding logging/error-handling when confidence was too low for a real fix,
not naming a root-cause file the way DiagnosisAgent does -- its "ground
truth" here is which file(s) its own additions targeted, not
diagnosis_affected_file (which is never set on these incidents at all; see
export_error_clarity_regression_dataset.py's docstring for why).

Real Anthropic API calls, same "live test" convention as
eval_diagnosis_regression.py -- not part of the mocked pytest suite. Run
manually after changing ErrorClarityAgent, not on every push.

No historical-pinning concern here the way DiagnosisAgent's eval has:
ErrorClarityAgent reads files via GitHub's API at ref=PR_BASE (live
"staging" branch), same as it does in production -- it was never diagnosing
against a specific historical commit in the first place, so there's no
post-fix-HEAD mismatch to guard against. (If the target file has since
changed enough that the original code_before text no longer exists, that's
a real, informative FAIL, not a tooling artifact.)

Scoring: does ANY file in result.additions match a file the real PR
actually touched? ErrorClarityAgent's self_explanatory / flag_pattern-only
outcomes have no file to check -- scored as FAIL (no location identified),
same policy as the diagnosis eval's "no affected_file" case.
  PASS - at least one addition's file matches a real ground-truth file
  FAIL - no match, or no additions at all (self-explanatory / patterns-only)

Usage:
    python scripts/eval_error_clarity_regression.py
    python scripts/eval_error_clarity_regression.py --dataset /tmp/clarity.jsonl
    python scripts/eval_error_clarity_regression.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "error_clarity_regression.jsonl"


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
    from app.agents.error_clarity import ErrorClarityAgent
    from app.models.events import ErrorEvent, EventSource, IncidentState

    event_data = case["event"]
    event = ErrorEvent(
        source=EventSource(event_data.get("source", "cloudwatch")),
        error_type=event_data.get("error_type"),
        title=event_data.get("title", ""),
        description=event_data.get("description", ""),
        service=event_data.get("service", ""),
        metadata=event_data.get("metadata", {}),
    )
    incident = IncidentState(error_event=event)

    agent = ErrorClarityAgent()
    result = await agent.analyze(incident)

    truth = case["ground_truth"]
    truth_files = set(truth.get("affected_files") or [])
    candidate_files = {a.file for a in result.additions}
    hit = bool(candidate_files & truth_files)

    if hit:
        verdict, detail = "PASS", f"matched {candidate_files & truth_files}"
    elif result.self_explanatory:
        verdict, detail = "FAIL", "flagged self-explanatory — no file identified"
    elif not candidate_files:
        verdict, detail = "FAIL", f"no additions — {len(result.patterns)} pattern-only recommendation(s)"
    else:
        verdict, detail = "FAIL", f"identified {candidate_files}, real PR touched {truth_files}"

    return {
        "incident_id": case["incident_id"],
        "title": event.title,
        "verdict": verdict,
        "detail": detail,
        "ground_truth_files": sorted(truth_files),
        "current_files": sorted(candidate_files),
        "self_explanatory": result.self_explanatory,
        "num_patterns": len(result.patterns),
    }


async def _run(cases: list[dict[str, Any]], verbose: bool = True) -> list[dict[str, Any]]:
    results = []
    for i, case in enumerate(cases, 1):
        if verbose:
            print(f"[{i}/{len(cases)}] {case.get('incident_id')} — {case.get('event', {}).get('title', '')} ...", flush=True)
        try:
            result = await _replay_one(case)
        except Exception as exc:
            result = {
                "incident_id": case.get("incident_id"),
                "title": case.get("event", {}).get("title", ""),
                "verdict": "ERROR",
                "detail": f"replay raised: {exc}",
                "ground_truth_files": case.get("ground_truth", {}).get("affected_files", []),
                "current_files": [],
                "self_explanatory": None,
                "num_patterns": None,
            }
        if verbose:
            print(f"    -> {result['verdict']}: {result['detail']}", flush=True)
        results.append(result)
    return results


def _print_report(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No regression cases found. Run scripts/export_error_clarity_regression_dataset.py first.")
        return

    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    print(f"# ErrorClarityAgent regression check (N={len(results)})\n")
    print(f"PASS: {counts['PASS']}  FAIL: {counts['FAIL']}  ERROR: {counts['ERROR']}\n")

    for r in results:
        if r["verdict"] == "PASS":
            continue
        print(f"[{r['verdict']}] {r['incident_id']} — {r['title']}")
        print(f"    {r['detail']}")

    if counts["FAIL"] == 0 and counts["ERROR"] == 0:
        print("\nNo regressions found.")
    else:
        print(f"\n{counts['FAIL'] + counts['ERROR']} regression(s) found — see above.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(_DEFAULT_DATASET), help="Path to the regression JSONL")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a report")
    args = parser.parse_args()

    cases = _load_cases(Path(args.dataset))
    results = asyncio.run(_run(cases, verbose=not args.json))

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_report(results)

    has_regression = any(r["verdict"] in ("FAIL", "ERROR") for r in results)
    return 1 if has_regression else 0


if __name__ == "__main__":
    sys.exit(main())
