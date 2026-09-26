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

Provider failures (auth, billing, rate limit, 5xx) are recorded as INFRA,
not ERROR, and any INFRA case makes the whole run INVALID: it fails, but
says "rerun", not "regression". Auth and billing failures also stop the
shard, since every later call would fail the same way.

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
import faulthandler
import json
import signal
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import cost_meter  # noqa: E402  (stdlib-only module)

_PRODUCTION_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "diagnosis_regression.jsonl"
_SWEBENCH_DATASET = Path(__file__).resolve().parent.parent / "app" / "evals" / "swebench_diagnosis_regression.jsonl"

# PROVISIONAL -- not yet calibrated. The only noise evidence so far is one
# known flip (psf__requests-1142) on an identical rerun; there is no measured
# noise floor for this gate yet, because it has never completed. Calibrate by
# running the gate on unchanged main at least twice (workflow_dispatch) and
# setting this comfortably above the observed non-pass rate, the way
# eval_triage_full_regression.py's 6% was set. At N=56, 10% allows 6 cases.
MAX_NONPASS_RATE = 0.10


def _provider_failure(exc: BaseException) -> str | None:
    """Name the provider-side failure behind `exc`, or None if it's an
    ordinary replay error.

    These say nothing about DiagnosisAgent, so they must never be scored as
    regressions. Calibration run 36070479644 is why: Anthropic credits ran
    out mid-run, 25 of 56 cases raised "credit balance is too low", and the
    gate reported a 30-case regression on unchanged main. Duck-types
    `status_code` the same way AlertingService.check_llm_provider_error does,
    since the raw SDK and the LiteLLM gateway raise different exception
    types for the same HTTP status.
    """
    status = getattr(exc, "status_code", None)
    if status == 401:
        return "auth"
    if status == 400 and "credit balance" in str(exc).lower():
        return "billing"
    if status == 429:
        return "rate_limit"
    if isinstance(status, int) and status >= 500:
        return "provider_outage"
    return None


# A non-PASS case is replayed this many more times before it counts. Two
# valid calibration runs on unchanged main showed 16 of 56 known-good cases
# failing at least once, with only 2 failing both times -- failures rotate
# between cases, so a case that fails ~20% of the time independently fails
# twice only ~4% of the time, while a genuinely broken case still fails
# both. Costs roughly one extra replay per ~4 cases at today's noise.
RETRIES = 1

# Auth and billing failures fail every later call too, so a shard stops
# replaying once it sees one instead of burning an hour on certain errors.
# Rate limits and 5xx can clear up, so those only mark the one case.
_FATAL_PROVIDER_FAILURES = {"auth", "billing"}


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


# A single replay normally takes 5-15 min. Run 36081331749's shard 4 hit a
# billing error mid-case and then waited silently for 93 min until the job's
# 150-min timeout killed it, taking the whole shard's results with it and
# leaving no trace of where it was stuck. Bounding each attempt keeps one
# hang from costing a shard, and the stack dump says where to look.
CASE_TIMEOUT_SECONDS = 30 * 60
# Backstop for a hang outside any one case (setup, result writing): dump
# every thread's stack shortly before replay jobs' 150-min timeout.
SHARD_STACK_DUMP_SECONDS = 140 * 60


def _await_chain(task: asyncio.Task) -> list[str]:
    """file:line of each coroutine the task is suspended in, outermost first.

    Task.print_stack() shows only the top frame of a suspended coroutine;
    walking cr_await reaches the call that is actually blocked (e.g. a
    subprocess wait or an HTTP read)."""
    frames, coro = [], task.get_coro()
    while coro is not None:
        f = getattr(coro, "cr_frame", None) or getattr(coro, "gi_frame", None)
        if f is not None:
            frames.append(f"{f.f_code.co_filename}:{f.f_lineno} in {f.f_code.co_name}")
        coro = getattr(coro, "cr_await", None) or getattr(coro, "gi_yieldfrom", None)
    return frames


async def _replay_attempt(replay, item, github, base: dict[str, Any]) -> dict[str, Any]:
    task = asyncio.ensure_future(replay(item, github))
    done, _ = await asyncio.wait({task}, timeout=CASE_TIMEOUT_SECONDS)
    if not done:
        print(f"    TIMEOUT after {CASE_TIMEOUT_SECONDS / 60:.0f} min -- the replay is "
              f"suspended at:", flush=True)
        for frame in _await_chain(task) or ["<no coroutine frames>"]:
            print(f"      {frame}", flush=True)
        task.cancel()
        # Bounded: if cancellation itself blocks (a cleanup `finally` awaiting
        # the same stuck call), abandon the task rather than hang here too.
        await asyncio.wait({task}, timeout=60)
        return {**base, "verdict": "TIMEOUT", "infra_kind": None,
                "detail": f"replay did not finish within {CASE_TIMEOUT_SECONDS / 60:.0f} min"}
    try:
        return task.result()
    except Exception as exc:
        kind = _provider_failure(exc)
        return {**base, "verdict": "INFRA" if kind else "ERROR", "infra_kind": kind,
                "detail": f"replay raised: {exc}"}


async def _run_suite(suite: str, items: list[dict[str, Any]], replay,
                     base_of, label_of, trajectory_dir: Path | None = None) -> list[dict[str, Any]]:
    """Replay each item, retrying a non-PASS once (see RETRIES).

    A case only counts as non-passing if every attempt fails. The verdict
    kept is the last attempt's; `attempts` records all of them, and
    `flaky` marks a case that passed only on retry.
    """
    from app.services.github import GitHubService

    if not items:
        return []
    github = GitHubService()
    results = []
    fatal = None
    for i, item in enumerate(items, 1):
        base = base_of(item)
        if fatal:
            results.append({**base, "verdict": "INFRA", "infra_kind": fatal, "suite": suite,
                            "attempts": [], "flaky": False,
                            "detail": f"not replayed: earlier {fatal} failure"})
            continue
        print(f"[{suite} {i}/{len(items)}] {label_of(item)} ...", flush=True)
        attempts = []
        # One meter per case, across all attempts: a retry is part of what
        # the case cost the gate.
        with cost_meter.metered() as meter:
            for attempt in range(1 + RETRIES):
                if attempt:
                    print(f"    retrying ({attempt}/{RETRIES}) ...", flush=True)
                sink: list[dict] = []
                attempt_replay = replay
                if trajectory_dir is not None:
                    async def attempt_replay(it, gh, _sink=sink):
                        return await replay(it, gh, trajectory_sink=_sink)
                result = await _replay_attempt(attempt_replay, item, github, base)
                attempts.append(result["verdict"])
                if trajectory_dir is not None and sink:
                    trajectory_dir.mkdir(parents=True, exist_ok=True)
                    record = {**sink[0], "attempt": attempt + 1,
                              "verdict": result["verdict"], "detail": result["detail"]}
                    name = f"{base_of(item).get('instance_id') or base_of(item).get('incident_id')}"
                    (trajectory_dir / f"{name}__attempt{attempt + 1}.json").write_text(
                        json.dumps(record, indent=1, default=str))
                print(f"    -> {result['verdict']}: {result['detail']}", flush=True)
                # TIMEOUT isn't retried: a hang tends to repeat, and a second
                # 30-min wait would push the shard toward its job timeout.
                if result["verdict"] in ("PASS", "INFRA", "TIMEOUT"):
                    break
        cost = meter.summary()
        print(f"    cost: ${cost['cost_usd']} over {cost['calls']} LLM calls", flush=True)
        if result.get("infra_kind") in _FATAL_PROVIDER_FAILURES:
            fatal = result["infra_kind"]
        results.append({**result, "suite": suite, "attempts": attempts, "cost": cost,
                        "flaky": result["verdict"] == "PASS" and len(attempts) > 1})
    return results


async def _run_production_suite(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from scripts.eval_diagnosis_regression import _replay_one

    return await _run_suite(
        "production", cases, _replay_one,
        base_of=lambda c: {"incident_id": c.get("incident_id"),
                           "title": c.get("event", {}).get("title", c.get("incident_id"))},
        label_of=lambda c: c.get("event", {}).get("title", c.get("incident_id")),
    )


async def _run_swebench_suite(instances: list[dict[str, Any]],
                              trajectory_dir: Path | None = None) -> list[dict[str, Any]]:
    from scripts.eval_swebench_diagnosis import _replay_one

    return await _run_suite(
        "swebench", instances, _replay_one,
        base_of=lambda x: {"instance_id": x.get("instance_id"), "repo": x.get("repo")},
        label_of=lambda x: f"{x['instance_id']} ({x['repo']})",
        trajectory_dir=trajectory_dir,
    )


def _print_cost(results: list[dict[str, Any]]) -> None:
    """Measured spend for the run -- printed for invalid runs too, since a
    run that dies on a billing error is exactly when the number matters."""
    costs = [r["cost"] for r in results if r.get("cost")]
    if not costs:
        return
    priced = [c["cost_usd"] for c in costs if c["cost_usd"] is not None]
    unpriced = sorted({m for c in costs for m in c["unpriced_models"]})
    by_type = {k: sum(c["by_billing_type"][k] for c in costs)
               for k in ("input", "output", "cache_write", "cache_read")}
    tokens = {k: sum(c["tokens"][k] for c in costs)
              for k in ("input", "output", "cache_write", "cache_read")}
    all_input = tokens["input"] + tokens["cache_write"] + tokens["cache_read"]
    total = sum(priced)
    print(f"\n## Cost (measured, {len(costs)} cases replayed)\n")
    print(f"Total: ${total:.2f}  |  per case: mean ${total / len(priced):.3f}, "
          f"max ${max(priced):.3f}" if priced else "Total: unknown")
    print("By billing type: " + ", ".join(f"{k} ${v:.2f}" for k, v in by_type.items()))
    if all_input:
        print(f"Cache hit rate (cache reads / all input tokens): "
              f"{tokens['cache_read'] / all_input:.1%}")
    if unpriced:
        print(f"WARNING: no price for {unpriced} -- totals exclude those calls.")


def _print_report(results: list[dict[str, Any]]) -> bool:
    """Returns True if the gate should FAIL (non-pass rate exceeds the
    threshold, or the run is invalid)."""
    _print_cost(results)
    infra = [r for r in results if r["verdict"] == "INFRA"]
    if infra:
        # Refuse to score at all, rather than scoring only the clean cases:
        # if an LLM call inside the ReAct loop hits the same failure and the
        # agent degrades to escalate instead of raising, that case shows up
        # as an ordinary FAIL -- so every failure from the same run is suspect.
        print(f"\n# DiagnosisAgent regression gate: RUN INVALID (N={len(results)})\n")
        print(f"{len(infra)} case(s) hit a provider-side failure (auth, billing, rate "
              f"limit, or outage) -- not a DiagnosisAgent regression. Not scored.")
        for kind in sorted({r["infra_kind"] for r in infra}):
            n = sum(r["infra_kind"] == kind for r in infra)
            print(f"  - {kind}: {n} case(s)")
        print("\nFix the provider issue (e.g. top up credits) and rerun the gate.")
        return True

    total = len(results)
    non_pass = [r for r in results if r["verdict"] != "PASS"]
    rate = len(non_pass) / total if total else 0.0
    max_allowed = max(1, round(total * MAX_NONPASS_RATE))

    flaky = [r for r in results if r.get("flaky")]

    print(f"\n# DiagnosisAgent regression gate (N={total})\n")
    print(f"PASS: {total - len(non_pass)} (of which {len(flaky)} only on retry)  "
          f"NOT-PASS: {len(non_pass)}  "
          f"({rate:.1%}, threshold {MAX_NONPASS_RATE:.0%} / max {max_allowed} cases)\n")
    if flaky:
        print("Passed only on retry (noise, not counted -- worth watching if a case recurs):")
        for r in flaky:
            print(f"  [{r['suite']}] {r.get('incident_id') or r.get('instance_id')}: {r['attempts']}")
        print()
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
    parser.add_argument("--trajectories-out", type=Path, metavar="DIR",
                        help="write one trajectory record per SWE-bench case attempt to DIR "
                             "(prompt composition + usage per LLM call, tool calls, cost); "
                             "input for scripts/analyze_cost_by_source.py")
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

    if of > 1:
        faulthandler.dump_traceback_later(SHARD_STACK_DUMP_SECONDS, exit=False)
        faulthandler.register(signal.SIGTERM, all_threads=True)

    all_production = _load_production()
    production = _select_shard(all_production, shard, of)
    swebench = _select_shard(_load_swebench(), shard, of, offset=len(all_production))
    if of > 1:
        print(f"Shard {shard}/{of}: {len(production)} production + {len(swebench)} SWE-bench cases", flush=True)

    results = (await _run_production_suite(production)
               + await _run_swebench_suite(swebench, trajectory_dir=args.trajectories_out))

    if args.results_out:
        args.results_out.parent.mkdir(parents=True, exist_ok=True)
        args.results_out.write_text(json.dumps({
            "shard": shard, "of": of,
            "expected": len(production) + len(swebench),
            "results": results,
        }, indent=2))

    # A shard gives no verdict of its own: the threshold only means
    # something over the whole set, so --aggregate decides pass/fail.
    # (Printing a per-shard "Gate FAILS" from a 7-case count read like a
    # regression verdict when it wasn't one.)
    if of > 1:
        counts = {v: sum(r["verdict"] == v for r in results) for v in sorted({r["verdict"] for r in results})}
        flaky = sum(bool(r.get("flaky")) for r in results)
        print(f"\nShard {shard}/{of} done: {counts}, {flaky} passed only on retry. "
              f"No verdict here -- the aggregate job applies the threshold.")
        return 0
    return 1 if _print_report(results) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
