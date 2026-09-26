"""
Run the harness optimizer on DiagnosisAgent: a long-running, resumable,
budget-capped loop (app/harness_optimizer/loop.py).

    # start, or resume: the same command continues a stopped run
    python scripts/optimize_harness.py --run-dir runs/harness/2026-09-26 --budget 120

    # where is it, what has it tried, what has it spent
    python scripts/optimize_harness.py --run-dir runs/harness/2026-09-26 --status

    # the full unattended campaign: smoke stage, 3-trial baseline, rounds at 2
    # trials, then the held-out comparison and report, seeded with a pilot's history
    python scripts/optimize_harness.py --run-dir runs/harness/r5 --budget 250 --rounds 8 \
        --trials 2 --calibration-trials 3 --smoke --final --final-trials 3 \
        --parallel 10 --lane-width 4 --seed-history runs/harness/r4-20260925/history.jsonl

Real API spend: every round evaluates a candidate on the evolve set
(~17 cases, ~$35 uncached per trial of each). Round 0 runs the starting
harness twice per case to calibrate the noise band. The --budget cap is
enforced in code before every case.

The run is configured from the committed split (app/evals/harness_split.json):
evolve cases and guards, and a critic denylist built from EVERY split case
(evolve and held-out), every repo, and every file a true fix touched. An edit
that names any of them is rejected before it costs anything. Held-out cases
are never seen by the proposer; with --final they are evaluated once, after
the last round, for the report (report.py).

Accepted edits land in <run-dir>/proposals/rN/ as a diff to review. Nothing
here modifies app/agents/harness/ in the repo.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SPLIT = ROOT / "app" / "evals" / "harness_split.json"
CASES = ROOT / "app" / "evals" / "swebench_verified_sample.jsonl"   # every split case, incl. the hard tier
DEFAULT_LLM = "claude-opus-5"


HELDOUT_EXTRA = ROOT / "app" / "evals" / "swebench_heldout_extra.jsonl"


def critic_patterns(split: dict, cases_paths: tuple[Path, ...] = (CASES, HELDOUT_EXTRA)) -> list[tuple[str, str]]:
    from app.harness_optimizer.critic import domain_patterns
    from scripts.eval_swebench_diagnosis import _touched_files

    ids = sorted(split["case_stats"])
    repos = sorted({s["repo"] for s in split["case_stats"].values()})
    paths: set[str] = set()
    for cases_path in cases_paths:
        if not cases_path.exists():
            continue
        for line in cases_path.read_text().splitlines():
            if not line.strip():
                continue
            inst = json.loads(line)
            if inst["instance_id"] in split["case_stats"]:
                paths |= _touched_files(inst["patch"])
    return domain_patterns(ids, repos, sorted(paths))


def case_lanes(split: dict) -> dict[str, str]:
    """One lane per repo: replays of the same repo share a base clone."""
    return {cid: s["repo"] for cid, s in split["case_stats"].items()}


def heldout_cases(split: dict) -> list[str]:
    return [c for tier in ("failing", "stable", "hard", "extended") for c in split["heldout"].get(tier, [])]


def smoke_cases(split: dict) -> list[str]:
    """The 4 guards (cheap, should pass) and the first failing case: 5 repos."""
    return split["evolve"]["guards"] + split["evolve"]["failing"][:1]


def build_config(split: dict, budget: float, rounds: int, trials: int, parallel: int = 1,
                 calibration_trials: int = 2, lane_width: int = 1, smoke: bool = False,
                 final: bool = False, final_trials: int = 3):
    from app.harness_optimizer.loop import OptimizerConfig

    return OptimizerConfig(
        evolve_cases=split["evolve"]["failing"] + split["evolve"].get("hard", []),
        guard_cases=split["evolve"]["guards"],
        budget_usd=budget, trials=trials, max_rounds=rounds,
        calibration_trials=calibration_trials,
        parallel_lanes=parallel, case_lanes=case_lanes(split), lane_width=lane_width,
        smoke_cases=smoke_cases(split) if smoke else [],
        final_cases=heldout_cases(split) if final else [], final_trials=final_trials,
        # Not the split's estimated_cost_usd_uncached: that's the uncached
        # Sonnet 4.6 gate era (~$2/case, 17x the measured $0.121/trial), and
        # reserving it per lane stopped a run at $0 spent. The loop's default
        # is used until the run measures its own cases.
    )


def make_llm(model: str):
    from app.services.llm import LLMService

    svc = LLMService(model=model)

    async def llm(system: str, prompt: str) -> str:
        return await svc.complete([{"role": "user", "content": prompt}], system=system)

    return llm


def status(run_dir: Path) -> int:
    from app.harness_optimizer.history import EditHistory
    from app.harness_optimizer.state import RunDir

    run = RunDir(run_dir)
    if not run.exists():
        print(f"no run at {run_dir}")
        return 1
    s = run.load()
    print(f"run {s.run_id}: round {s.round}, phase {s.phase}, spent ${s.spent_usd:.2f} "
          f"of ${s.config['budget_usd']:.2f}")
    print(f"S* {s.S_star}, delta {s.delta}, accepted {s.accepted or '-'}")
    if s.stop_reason:
        print(f"stopped: {s.stop_reason}")
    t = s.timing or {}
    for i, sess in enumerate(t.get("sessions", []), 1):
        print(f"session {i}: {sess['start']} -> {sess.get('end') or sess.get('last_seen', '?')}, "
              f"{sess.get('seconds', 0) / 3600:.2f}h, {sess.get('replays', 0)} replays, "
              f"ended: {sess.get('ended_because', 'running')}")
    if s.health.get("last_check"):
        lc = s.health["last_check"]
        print(f"tripwire checks: {s.health['checks']}, last {lc['time']}: {lc['problems'] or 'ok'}")
    print()
    print(EditHistory(run.root / "history.jsonl").summary())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--budget", type=float, help="hard cap in USD for this run (on resume: raise the cap)")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--trials", type=int, default=1, help="trials per case per candidate")
    parser.add_argument("--model", default=DEFAULT_LLM, help="proposer and critic model")
    parser.add_argument("--parallel", type=int, default=None,
                        help="repos evaluated concurrently (cases of one repo always run in "
                             "sequence). Operational, so it can be changed on resume. Default 1.")
    parser.add_argument("--calibration-trials", type=int, default=2, help="round 0 trials per case")
    parser.add_argument("--lane-width", type=int, default=1,
                        help="concurrent cases per repo (separate worktrees). Operational.")
    parser.add_argument("--smoke", action="store_true", help="replay 5 cases once before round 0")
    parser.add_argument("--final", action="store_true",
                        help="after the rounds, run original vs final harness on the held-out set")
    parser.add_argument("--final-trials", type=int, default=3)
    parser.add_argument("--seed-history", type=Path,
                        help="a pilot run's history.jsonl, given to the proposer as notes (new runs only)")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    args.rounds_given = any(a == "--rounds" or a.startswith("--rounds=") for a in sys.argv[1:])

    if args.status:
        return status(args.run_dir)

    from dotenv import load_dotenv

    from app.agents.harness import DEFAULT_ROOT
    from app.harness_optimizer.evaluator import ReplayEvaluator
    from app.harness_optimizer.loop import Optimizer
    from app.harness_optimizer.state import RunDir

    load_dotenv()
    run = RunDir(args.run_dir)
    split = json.loads(SPLIT.read_text())
    if run.exists():
        from app.harness_optimizer.loop import OptimizerConfig
        cfg = OptimizerConfig(**run.load().config)
        # Method settings (trials, cases, acceptance) come from the run and
        # can't change mid-run; parallelism is operational and can, and the
        # budget and round limits can be raised.
        if args.parallel is not None:
            cfg.parallel_lanes = args.parallel
        if any(a.startswith("--lane-width") for a in sys.argv[1:]):
            cfg.lane_width = args.lane_width
        # Extending a run: more rounds may be added on resume (never fewer,
        # never other method settings). A run that stopped because it hit its
        # round limit is reopened at the next round.
        state = run.load()
        if args.rounds_given and args.rounds > cfg.max_rounds:
            cfg.max_rounds = args.rounds
            state.config["max_rounds"] = args.rounds
            if state.phase == "done" and (state.stop_reason or "").startswith(("completed", "round 0 only")):
                state.phase, state.stop_reason = "propose", None
            run.save(state)
            print(f"extended to {args.rounds} rounds")
        # The budget cap may be raised on resume (never lowered). A run stopped
        # by the cap keeps its phase, so it continues where it stopped.
        if args.budget is not None and args.budget > cfg.budget_usd:
            print(f"budget raised from ${cfg.budget_usd:.2f} to ${args.budget:.2f}")
            cfg.budget_usd = args.budget
            state.config["budget_usd"] = args.budget
            run.save(state)
        cfg.case_lanes = cfg.case_lanes or case_lanes(split)
        print(f"resuming {args.run_dir} (method config from the run's state; "
              f"parallel lanes = {cfg.parallel_lanes})")
    else:
        if args.budget is None:
            parser.error("--budget is required to start a new run")
        cfg = build_config(split, args.budget, args.rounds, args.trials, args.parallel or 1,
                           args.calibration_trials, args.lane_width, args.smoke, args.final,
                           args.final_trials)
        if args.seed_history:
            seed_history(args.seed_history, args.run_dir)
    opt = Optimizer(args.run_dir, DEFAULT_ROOT / "diagnosis", cfg, ReplayEvaluator(),
                    make_llm(args.model), make_llm(args.model), critic_patterns(split))
    state = asyncio.run(opt.run_until_stopped())
    print(f"\nstopped at round {state.round}, phase {state.phase}: {state.stop_reason}")
    print(f"spent ${state.spent_usd:.2f}; accepted {state.accepted or 'nothing'}", flush=True)
    return 0 if state.phase == "done" else 2


def seed_history(pilot: Path, run_dir: Path) -> None:
    """Copy a pilot run's judged candidates into this run's history, marked so
    the proposer knows they were measured elsewhere and are NOT part of this
    run's harness (an accepted pilot edit is not applied here)."""
    from dataclasses import replace

    from app.harness_optimizer.history import EditHistory

    hist = EditHistory(run_dir / "history.jsonl")
    if hist.entries():
        return
    for e in EditHistory(pilot).entries():
        hist.append(replace(e, round=0, candidate_id=f"pilot-{e.candidate_id}",
                            outcome=f"pilot_{e.outcome}",
                            reason=f"[pilot run {pilot.parent.name}, 1 trial, not applied to this "
                                   f"run's harness] {e.reason}"))


if __name__ == "__main__":
    code = main()
    # Hard exit: a stopped run must not linger. One did, hung for 18 hours after
    # recording a clean stop, on threads or subprocess pipes a normal
    # interpreter shutdown waits for. State is already saved at this point.
    sys.stdout.flush()
    sys.stderr.flush()
    import os
    os._exit(code)
