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

Retrieval paths (read this before citing a pass rate):
DiagnosisAgent has four ways to find code, and the original 56% run exercised
one of them. `rag=None` was passed here, so hybrid_search was off; the
stack-trace fast path required a "/app/" prefix and a JS/TS extension, so it
fired on 0 of 100 all-Python instances; and the tree-sitter call graph behind
find_callers had no Python grammar, so it returned "no callers found" every
time. Only direct code search (grep_codebase / get_file_contents / GitHub
search_code) was live. The other three have since been fixed; `--rag` is
opt-in so the with/without comparison stays controlled. Any pass rate from a
default run should be cited as "direct code search only".

Usage:
    python scripts/eval_swebench_diagnosis.py                   # baseline: no RAG
    python scripts/eval_swebench_diagnosis.py --rag             # all four paths live
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


async def _build_instance_rag(instance_id: str, worktree_path: str) -> tuple[Any, float]:
    """Index one pinned worktree into its own throwaway collection.

    Per-instance, not per-repo: SWE-bench instances from the same repo sit at
    different `base_commit`s, so a shared index would retrieve code that isn't
    in the worktree the agent is reading. Collections are namespaced by
    instance_id and cleared afterwards.

    Returns (rag_service, seconds_spent). Returns (None, 0.0) on any failure —
    a broken index must degrade to the grep-only path, not abort the instance,
    so a partial run stays comparable to the no-RAG baseline.
    """
    import time

    start = time.perf_counter()
    try:
        from app.services.rag import RAGService

        rag = RAGService(collection_name=f"swebench_{instance_id}".replace("__", "_"))
        await rag.index_directory(worktree_path, root=worktree_path)
        return rag, time.perf_counter() - start
    except Exception as exc:
        print(f"    [rag] indexing failed for {instance_id}: {exc}", flush=True)
        return None, time.perf_counter() - start


def _capture_steps(agent: Any, sink: list[Any]) -> None:
    """Wrap agent.run() so the ReAct step list survives the call.

    DiagnosisAgent.diagnose() does `await self.run(prompt)` and discards the
    AgentResult (diagnosis.py:1662), so the per-turn tool sequence exists only
    inside that call. Everything persisted elsewhere is lossy: `agent_runs`
    keeps a tool-call *count*, logs/agent_sessions.jsonl keeps human-readable
    status strings, and Langfuse truncates every tool output to 500 characters
    (tracing.py:218) — which is fine for debugging and useless for trajectory
    analysis.

    Done here as an eval-only wrapper rather than an attribute on BaseAgent
    deliberately: touching base.py triggers both regression gates (~$45 for the
    diagnosis replay) and changes behaviour for all 7 pipeline agents, for a
    capability only the harness needs today. Stage 2's optimizer will need
    trajectory capture in production too — promote it to BaseAgent then, with
    the gate cost paid once and on purpose.
    """
    original = agent.run

    async def capturing(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        sink.append(result)
        return result

    agent.run = capturing


def _steps_records(instance_id: str, results: list[Any], max_chars: int) -> list[dict]:
    """Flatten captured AgentResults into one record per tool call.

    Shape matches what scripts/analyze_tool_call_headroom.py --from-file reads,
    so the headroom analysis runs off a real 100-instance sweep instead of
    whatever happens to be recent in Langfuse.
    """
    records = []
    for result in results:
        for step in getattr(result, "steps", []) or []:
            if not step.action:
                continue        # final answer step, no tool call
            observation = step.observation or ""
            records.append({
                "trace_id": instance_id,
                "iteration": step.iteration,
                "name": step.action,
                "input": step.action_input,
                "output": observation[:max_chars],
                "output_truncated": len(observation) > max_chars,
                "thought": (step.thought or "")[:2000],
            })
    return records


async def _replay_one(instance: dict[str, Any], github: Any, use_rag: bool = False,
                      steps_sink: list[dict] | None = None,
                      steps_max_chars: int = 20000) -> dict[str, Any]:
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

    # rag defaults to None, which is how the original 56% baseline was measured:
    # _search_codebase degrades to "RAG not configured" and DiagnosisAgent runs
    # on direct code search alone. That was a deliberate shortcut (no per-repo
    # index to build) whose consequence went unrecorded -- 1 of the agent's 4
    # retrieval paths was active for the whole evaluation. --rag builds a real
    # per-instance index so the two can be compared directly.
    rag = None
    index_seconds = 0.0
    if use_rag:
        # ensure_fresh() is what materialises the pinned worktree; diagnose()
        # calls it too, but the index has to be built against real files on
        # disk, so it has to happen first. Idempotent, so the later call is
        # a no-op.
        await pinned_repo.ensure_fresh()
        rag, index_seconds = await _build_instance_rag(
            instance["instance_id"], str(pinned_repo.local_path)
        )

    agent = DiagnosisAgent(github=github, local_repo=pinned_repo, owner=owner, repo=repo, rag=rag)
    # DiagnosisAgent.__init__ hardcodes LLMService() -- Sonnet from
    # config/llm_routing.json's "defaults" section -- and has no llm= override,
    # so without this line "routing.diagnosis.model" (the per-task override,
    # e.g. claude-sonnet-5) is silently never read here. incident_loop.py is
    # the only place that currently applies this override
    # (self._diagnosis._llm = llm_gateway.get_llm_service_for("diagnosis"));
    # every eval script constructing DiagnosisAgent directly needs the same
    # line, or it measures whatever model "defaults" happens to name, not the
    # one actually configured for the diagnosis task.
    from app.services.llm_gateway import llm_gateway
    agent._llm = llm_gateway.get_llm_service_for("diagnosis")
    captured: list[Any] = []
    if steps_sink is not None:
        _capture_steps(agent, captured)
    try:
        result = await agent.diagnose(incident)
    finally:
        if steps_sink is not None:
            steps_sink.extend(
                _steps_records(instance["instance_id"], captured, steps_max_chars)
            )
        await pinned_repo.remove_worktree()
        if rag is not None:
            # Throwaway collection -- one per instance would otherwise accumulate.
            try:
                rag.clear()
            except Exception:
                pass

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
        "rag_enabled": rag is not None,
        "index_seconds": round(index_seconds, 1),
    }


async def _run(instances: list[dict[str, Any]], verbose: bool = True,
               use_rag: bool = False, steps_sink: list[dict] | None = None,
               steps_max_chars: int = 20000) -> list[dict[str, Any]]:
    from app.services.github import GitHubService

    github = GitHubService()
    results = []
    for i, instance in enumerate(instances, 1):
        if verbose:
            print(f"[{i}/{len(instances)}] {instance['instance_id']} ({instance['repo']}) ...", flush=True)
        try:
            result = await _replay_one(
                instance, github, use_rag=use_rag,
                steps_sink=steps_sink, steps_max_chars=steps_max_chars,
            )
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
                "rag_enabled": use_rag,
                "index_seconds": 0.0,
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
    parser.add_argument(
        "--rag",
        action="store_true",
        help=(
            "Build a per-instance embedding index over the pinned worktree and give "
            "DiagnosisAgent its hybrid_search path. Off by default, which reproduces "
            "the original 56%% baseline (direct code search only)."
        ),
    )
    parser.add_argument(
        "--steps-out",
        default=None,
        metavar="PATH",
        help=(
            "Write every ReAct tool call (action, full args, untruncated observation) "
            "to a JSONL at PATH. Feed it to scripts/analyze_tool_call_headroom.py "
            "--from-file, and reuse it as trajectory input for harness optimisation."
        ),
    )
    parser.add_argument(
        "--steps-max-chars", type=int, default=20000, metavar="N",
        help=(
            "Per-observation cap in the steps JSONL (default 20000). Langfuse caps at "
            "500, which is too lossy for dependency analysis; full file contents would "
            "make the file enormous. Records set output_truncated when the cap bites."
        ),
    )
    args = parser.parse_args()

    instances = _load_instances(Path(args.dataset), limit=args.limit)
    steps_sink: list[dict] | None = [] if args.steps_out else None
    results = asyncio.run(_run(
        instances, verbose=not args.json, use_rag=args.rag,
        steps_sink=steps_sink, steps_max_chars=args.steps_max_chars,
    ))

    if steps_sink is not None:
        out = Path(args.steps_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w") as f:
            for rec in steps_sink:
                f.write(json.dumps(rec) + "\n")
        truncated = sum(1 for r in steps_sink if r["output_truncated"])
        print(f"\nWrote {len(steps_sink)} tool-call steps to {out}"
              f" ({truncated} outputs hit the {args.steps_max_chars}-char cap)",
              file=sys.stderr)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        _print_report(results)

    has_error = any(r["verdict"] == "ERROR" for r in results)
    return 1 if has_error else 0


if __name__ == "__main__":
    sys.exit(main())
