"""
Replay every case in app/evals/pipeline_regression.jsonl through the CURRENT
DiagnosisAgent and check it still finds what it found the first time.

Real motivation: there was no way to check "did my prompt/logic change to
DiagnosisAgent break how it diagnoses problems" short of waiting for a new
real incident and hoping it goes well. This replays the incidents that
already went on to merge a real fix -- known-good ground truth -- against
whatever DiagnosisAgent is *right now*.

Real Anthropic API calls (Sonnet), same as scripts/eval_rag.py's existing
"live test" pattern -- not part of the always-mocked pytest suite, since
LLM output isn't deterministic and this is specifically testing real model
behavior. Run manually after changing an agent, not on every push.

Deliberately diagnosis-only, not full fix+sandbox: FixGenerationAgent has no
dry-run mode, so running it here would open a real PR against the live
target repo on every regression check. See DECISIONS.md.

Scoring is structural, not exact-match (LLM output isn't deterministic):
  PASS   - same affected_file, confidence within CONFIDENCE_TOLERANCE
  DRIFT  - same affected_file, confidence moved beyond tolerance (not a
           hard fail -- flags a real behavior shift worth looking at)
  FAIL   - different affected_file, or no file identified this time

Usage:
    python scripts/eval_pipeline_regression.py
    python scripts/eval_pipeline_regression.py --dataset /tmp/regression.jsonl
    python scripts/eval_pipeline_regression.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "pipeline_regression.jsonl"

CONFIDENCE_TOLERANCE = 0.15


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
    from app.agents.diagnosis import DiagnosisAgent
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

    agent = DiagnosisAgent()
    result = await agent.diagnose(incident)

    truth = case["ground_truth"]
    verdict = _score(truth, result)

    return {
        "incident_id": case["incident_id"],
        "title": event.title,
        "verdict": verdict["status"],
        "detail": verdict["detail"],
        "ground_truth_file": truth.get("affected_file"),
        "current_file": result.affected_file,
        "ground_truth_confidence": truth.get("confidence"),
        "current_confidence": result.confidence,
    }


def _score(truth: dict[str, Any], result: Any) -> dict[str, str]:
    truth_file = truth.get("affected_file")
    if result.affected_file != truth_file:
        return {
            "status": "FAIL",
            "detail": f"affected_file changed: {truth_file!r} -> {result.affected_file!r}",
        }

    truth_confidence = truth.get("confidence")
    if truth_confidence is not None and abs(result.confidence - truth_confidence) > CONFIDENCE_TOLERANCE:
        return {
            "status": "DRIFT",
            "detail": (
                f"same file, confidence moved beyond tolerance: "
                f"{truth_confidence:.2f} -> {result.confidence:.2f}"
            ),
        }

    return {"status": "PASS", "detail": "matches ground truth"}


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
                "ground_truth_file": case.get("ground_truth", {}).get("affected_file"),
                "current_file": None,
                "ground_truth_confidence": case.get("ground_truth", {}).get("confidence"),
                "current_confidence": None,
            }
        if verbose:
            print(f"    -> {result['verdict']}: {result['detail']}", flush=True)
        results.append(result)
    return results


def _print_report(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No regression cases found. Run scripts/export_pipeline_regression_dataset.py first.")
        return

    counts = {"PASS": 0, "DRIFT": 0, "FAIL": 0, "ERROR": 0}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    print(f"# Diagnosis regression check (N={len(results)})\n")
    print(f"PASS: {counts['PASS']}  DRIFT: {counts['DRIFT']}  FAIL: {counts['FAIL']}  ERROR: {counts['ERROR']}\n")

    for r in results:
        if r["verdict"] == "PASS":
            continue
        print(f"[{r['verdict']}] {r['incident_id']} — {r['title']}")
        print(f"    {r['detail']}")

    if counts["FAIL"] == 0 and counts["ERROR"] == 0:
        print("\nNo regressions found." + (" Some confidence drift to review." if counts["DRIFT"] else ""))
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
