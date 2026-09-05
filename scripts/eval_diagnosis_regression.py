"""
Replay every case in app/evals/diagnosis_regression.jsonl through the CURRENT
DiagnosisAgent and check it still finds what it found the first time.

DiagnosisAgent-specific -- see scripts/eval_error_clarity_regression.py for
the sibling eval covering ErrorClarityAgent's observability PRs, which use a
different ground truth (which file the merged PR touched, not
diagnosis_affected_file) and a different notion of "correct."

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

Historical checkout, not live HEAD: every ground-truth case here is a bug
that WAS ALREADY FIXED (outcome == "fix_merged"), so diagnosing it against
the target repo's current HEAD is structurally guaranteed to fail --
DiagnosisAgent's own grounding gate correctly refuses to ground a diagnosis
against code that no longer has the bug. Each case instead resolves its
PR's pre-merge commit (merge_commit_sha's first parent) via the GitHub API
and diagnoses against an isolated git worktree pinned to that SHA
(LocalRepoService(pinned_sha=...)), so the repo state actually matches what
the bug looked like when it was real.

Scoring is structural, not exact-match (LLM output isn't deterministic):
  PASS   - same affected_file, confidence within CONFIDENCE_TOLERANCE
  DRIFT  - same affected_file, confidence moved beyond tolerance (not a
           hard fail -- flags a real behavior shift worth looking at)
  FAIL   - different affected_file, or no file identified this time

Usage:
    python scripts/eval_diagnosis_regression.py
    python scripts/eval_diagnosis_regression.py --dataset /tmp/regression.jsonl
    python scripts/eval_diagnosis_regression.py --json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "diagnosis_regression.jsonl"

CONFIDENCE_TOLERANCE = 0.15

_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _parse_pr_url(pr_url: str | None) -> tuple[str, str, int]:
    if not pr_url:
        raise ValueError("ground_truth.pr_url is required to resolve a pre-fix commit")
    m = _PR_URL_RE.search(pr_url)
    if not m:
        raise ValueError(f"cannot parse owner/repo/pr_number from pr_url: {pr_url!r}")
    owner, repo, pr_number = m.group(1), m.group(2), int(m.group(3))
    return owner, repo, pr_number


async def _resolve_pre_fix_sha(github: Any, owner: str, repo: str, pr_number: int) -> str:
    """The repo's SHA immediately before this PR's fix landed."""
    merge_sha = await github.get_pr_merge_commit_sha(owner, repo, pr_number)
    if not merge_sha:
        raise RuntimeError(f"PR #{pr_number} has no merge_commit_sha — was it actually merged?")
    return await github.get_commit_parent_sha(owner, repo, merge_sha)


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


async def _replay_one(case: dict[str, Any], github: Any) -> dict[str, Any]:
    from app.agents.diagnosis import DiagnosisAgent
    from app.models.events import ErrorEvent, EventSource, IncidentState
    from app.services.repo import LocalRepoService

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

    truth = case["ground_truth"]
    owner, repo, pr_number = _parse_pr_url(truth.get("pr_url"))
    pre_fix_sha = await _resolve_pre_fix_sha(github, owner, repo, pr_number)

    pinned_repo = LocalRepoService(owner, repo, pinned_sha=pre_fix_sha)
    agent = DiagnosisAgent(github=github, local_repo=pinned_repo)
    try:
        result = await agent.diagnose(incident)
    finally:
        await pinned_repo.remove_worktree()

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
        "pre_fix_sha": pre_fix_sha,
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
    from app.services.github import GitHubService

    github = GitHubService()
    results = []
    for i, case in enumerate(cases, 1):
        if verbose:
            print(f"[{i}/{len(cases)}] {case.get('incident_id')} — {case.get('event', {}).get('title', '')} ...", flush=True)
        try:
            result = await _replay_one(case, github)
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
        print("No regression cases found. Run scripts/export_diagnosis_regression_dataset.py first.")
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
