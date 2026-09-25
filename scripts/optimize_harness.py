"""
Run the harness optimizer on DiagnosisAgent: a long-running, resumable,
budget-capped loop (app/harness_optimizer/loop.py).

    # start, or resume: the same command continues a stopped run
    python scripts/optimize_harness.py --run-dir runs/harness/2026-09-26 --budget 120

    # where is it, what has it tried, what has it spent
    python scripts/optimize_harness.py --run-dir runs/harness/2026-09-26 --status

Real API spend: every round evaluates a candidate on the evolve set
(~17 cases, ~$35 uncached per trial of each). Round 0 runs the starting
harness twice per case to calibrate the noise band. The --budget cap is
enforced in code before every case.

The run is configured from the committed split (app/evals/harness_split.json):
evolve cases and guards, and a critic denylist built from EVERY split case
(evolve and held-out), every repo, and every file a true fix touched. An edit
that names any of them is rejected before it costs anything. Held-out cases
are never evaluated here; the final comparison is a separate step.

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
CASES = ROOT / "app" / "evals" / "swebench_diagnosis_regression.jsonl"
DEFAULT_LLM = "claude-opus-5"


def critic_patterns(split: dict, cases_path: Path = CASES) -> list[tuple[str, str]]:
    from app.harness_optimizer.critic import domain_patterns
    from scripts.eval_swebench_diagnosis import _touched_files

    ids = sorted(split["case_stats"])
    repos = sorted({s["repo"] for s in split["case_stats"].values()})
    paths: set[str] = set()
    for line in cases_path.read_text().splitlines():
        inst = json.loads(line)
        if inst["instance_id"] in split["case_stats"]:
            paths |= _touched_files(inst["patch"])
    return domain_patterns(ids, repos, sorted(paths))


def build_config(split: dict, budget: float, rounds: int, trials: int):
    from app.harness_optimizer.loop import OptimizerConfig

    return OptimizerConfig(
        evolve_cases=split["evolve"]["failing"], guard_cases=split["evolve"]["guards"],
        budget_usd=budget, trials=trials, max_rounds=rounds,
        default_case_cost_usd=split["estimated_cost_usd_uncached"]["evolve_eval_k1"]
        / max(1, len(split["evolve"]["failing"]) + len(split["evolve"]["guards"])),
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
    print()
    print(EditHistory(run.root / "history.jsonl").summary())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--budget", type=float, help="hard cap in USD for this run")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--trials", type=int, default=1, help="trials per case per candidate")
    parser.add_argument("--model", default=DEFAULT_LLM, help="proposer and critic model")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.status:
        return status(args.run_dir)

    from dotenv import load_dotenv

    from app.agents.harness import DEFAULT_ROOT
    from app.harness_optimizer.evaluator import ReplayEvaluator
    from app.harness_optimizer.loop import Optimizer
    from app.harness_optimizer.state import RunDir

    load_dotenv()
    run = RunDir(args.run_dir)
    if run.exists():
        cfg_json = run.load().config
        from app.harness_optimizer.loop import OptimizerConfig
        cfg = OptimizerConfig(**cfg_json)
        print(f"resuming {args.run_dir} (config from the run's state; flags ignored)")
    else:
        if args.budget is None:
            parser.error("--budget is required to start a new run")
        cfg = build_config(json.loads(SPLIT.read_text()), args.budget, args.rounds, args.trials)
    split = json.loads(SPLIT.read_text())
    opt = Optimizer(args.run_dir, DEFAULT_ROOT / "diagnosis", cfg, ReplayEvaluator(),
                    make_llm(args.model), make_llm(args.model), critic_patterns(split))
    state = asyncio.run(opt.run_until_stopped())
    print(f"\nstopped at round {state.round}, phase {state.phase}: {state.stop_reason}")
    print(f"spent ${state.spent_usd:.2f}; accepted {state.accepted or 'nothing'}")
    return 0 if state.phase == "done" else 2


if __name__ == "__main__":
    sys.exit(main())
