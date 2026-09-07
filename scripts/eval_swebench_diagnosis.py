"""
Replay DiagnosisAgent against real SWE-bench Verified instances --
localization-only: does it name the file the real fix actually touched?

Real motivation: every eval this codebase has for DiagnosisAgent so far is
self-sourced (AllInterviews' own production incidents). SWE-bench Verified
(github.com/SWE-bench, 500 human-validated real GitHub issues + merged PRs
across 12 real open-source repos) is a credible, third-party, non-self-graded
answer to "does this bug-localization approach generalize beyond one app."

Deliberately localization-only, not full fix+sandbox: FixGenerationAgent has
no dry-run mode (same constraint as scripts/eval_diagnosis_regression.py) AND
is hardcoded end-to-end to one Node/npm/Jest target app (owner/repo, live
branch tip, three literal source patches, Jest-shaped failure parsing) --
supporting arbitrary Python repos there is a separate, much bigger piece of
work. This only asks: did DiagnosisAgent's affected_file (+ secondary
targets) land on a file the real PR actually changed?

Real Anthropic + GitHub API calls per instance (Sonnet), same "live test"
convention as eval_rag.py / eval_diagnosis_regression.py -- not part of the
mocked pytest suite. Run manually, expect real cost/time: cloning a real
open-source repo per instance, then a full DiagnosisAgent ReAct loop.

Each instance is checked out at its own pinned base_commit via
LocalRepoService(pinned_sha=...) -- the exact mechanism built for
eval_diagnosis_regression.py, reused here across arbitrary repos instead of
one fixed target app (DiagnosisAgent now takes owner=/repo= overrides for
this -- see diagnosis.py).

Scoring is file-level only (SWE-bench Verified doesn't give a ground-truth
function name or confidence the way this codebase's own regression-eval
dataset does):
  PASS - affected_file (or any additional_fix_file/additional_fix_targets
         entry) matches a file the real patch touched
  FAIL - no match, or no file identified at all

Usage:
    python scripts/eval_swebench_diagnosis.py
    python scripts/eval_swebench_diagnosis.py --dataset /tmp/sample.jsonl --limit 5
    python scripts/eval_swebench_diagnosis.py --json
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

_DEFAULT_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "swebench_verified_sample.jsonl"

_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$", re.MULTILINE)


def _load_instances(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    instances = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                instances.append(json.loads(line))
    return instances[:limit] if limit else instances


def _touched_files(patch: str) -> set[str]:
    """Every file path named in a unified diff's 'diff --git a/... b/...' headers."""
    files: set[str] = set()
    for a_path, b_path in _DIFF_HEADER_RE.findall(patch):
        files.add(a_path)
        files.add(b_path)
    return files


async def _replay_one(instance: dict[str, Any], github: Any) -> dict[str, Any]:
    from app.agents.diagnosis import DiagnosisAgent
    from app.models.events import ErrorEvent, EventSource, IncidentState
    from app.services.repo import LocalRepoService

    owner, repo = instance["repo"].split("/", 1)
    base_commit = instance["base_commit"]
    truth_files = _touched_files(instance["patch"])

    # SWE-bench issues are plain text with no log_group/task_id -- DiagnosisAgent's
    # existing "log_group missing" prompt branch (skip steps 1-3, cap confidence
    # at 0.75) already handles this gracefully, same path exercised by any
    # AllInterviews incident with missing log metadata.
    event = ErrorEvent(
        source=EventSource.APPLICATION,
        error_type="GITHUB_ISSUE",
        title=instance["instance_id"],
        description=instance["problem_statement"],
        service=repo,
        metadata={},
    )
    incident = IncidentState(error_event=event)

    pinned_repo = LocalRepoService(owner, repo, pinned_sha=base_commit)
    # rag=None -- no per-repo embedding index to build; _search_codebase already
    # degrades gracefully ("RAG not configured") when rag is None.
    agent = DiagnosisAgent(github=github, local_repo=pinned_repo, owner=owner, repo=repo, rag=None)
    try:
        result = await agent.diagnose(incident)
    finally:
        await pinned_repo.remove_worktree()

    candidates = {f for f in (result.affected_file, result.additional_fix_file) if f}
    candidates |= {t.get("file") for t in (result.additional_fix_targets or []) if t.get("file")}
    normalized_truth = {f.lstrip("/") for f in truth_files}
    normalized_candidates = {c.lstrip("/") for c in candidates}
    hit = bool(normalized_candidates & normalized_truth)

    if hit:
        verdict, detail = "PASS", f"matched {normalized_candidates & normalized_truth}"
    elif not candidates:
        verdict, detail = "FAIL", "no affected_file identified — escalated or ungrounded"
    else:
        verdict, detail = "FAIL", f"identified {normalized_candidates or '{}'}, real fix touched {normalized_truth}"

    return {
        "instance_id": instance["instance_id"],
        "repo": instance["repo"],
        "verdict": verdict,
        "detail": detail,
        "truth_files": sorted(normalized_truth),
        "candidate_files": sorted(normalized_candidates),
        "confidence": result.confidence,
        "escalate": result.escalate,
    }


async def _run(instances: list[dict[str, Any]], verbose: bool = True) -> list[dict[str, Any]]:
    from app.services.github import GitHubService

    github = GitHubService()
    results = []
    for i, instance in enumerate(instances, 1):
        if verbose:
            print(f"[{i}/{len(instances)}] {instance['instance_id']} ({instance['repo']}) ...", flush=True)
        try:
            result = await _replay_one(instance, github)
        except Exception as exc:
            result = {
                "instance_id": instance.get("instance_id"),
                "repo": instance.get("repo"),
                "verdict": "ERROR",
                "detail": f"replay raised: {exc}",
                "truth_files": sorted(_touched_files(instance.get("patch", ""))),
                "candidate_files": [],
                "confidence": None,
                "escalate": None,
            }
        if verbose:
            print(f"    -> {result['verdict']}: {result['detail']}", flush=True)
        results.append(result)
    return results


def _print_report(results: list[dict[str, Any]]) -> None:
    if not results:
        print("No instances found. Run scripts/fetch_swebench_sample.py first.")
        return

    counts = {"PASS": 0, "FAIL": 0, "ERROR": 0}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    total = len(results)
    scored = counts["PASS"] + counts["FAIL"]
    pass_rate = counts["PASS"] / scored if scored else 0.0

    print(f"\n# SWE-bench Verified localization check (N={total})\n")
    print(f"PASS: {counts['PASS']}  FAIL: {counts['FAIL']}  ERROR: {counts['ERROR']}")
    print(f"Pass rate (excluding ERROR): {pass_rate:.0%}\n")

    by_repo: dict[str, list[str]] = {}
    for r in results:
        by_repo.setdefault(r["repo"], []).append(r["verdict"])
    print("By repo:")
    for repo, verdicts in sorted(by_repo.items()):
        p = verdicts.count("PASS")
        print(f"  {repo:30s} {p}/{len(verdicts)}")

    print()
    for r in results:
        if r["verdict"] == "PASS":
            continue
        print(f"[{r['verdict']}] {r['instance_id']} ({r['repo']})")
        print(f"    {r['detail']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=str(_DEFAULT_DATASET), help="Path to the SWE-bench sample JSONL")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N instances")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of a report")
    args = parser.parse_args()

    instances = _load_instances(Path(args.dataset), limit=args.limit)
    results = asyncio.run(_run(instances, verbose=not args.json))

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_report(results)

    has_error = any(r["verdict"] == "ERROR" for r in results)
    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
