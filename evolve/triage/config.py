"""
harness-evolve run configuration for TriageAgent: the agent profile, the evaluator and the case split.

    harness-evolve check evolve/triage/config.py --budget 5          # free pre-run checks, no model calls
    harness-evolve run evolve/triage/config.py --run-dir runs/triage-evolve-1 --budget 5

The optimizer edits a copy of app/agents/harness/triage/ (the prompt, the two tool descriptions, the turn
budget); each candidate is measured by run_case.py, one subprocess per case and trial. Accepted edits reach
production only through a normal PR and the triage gate.

Cases: the evolve set is a seeded sample of app/evals/triage_train.jsonl, stratified by label; the held-out set
is app/evals/triage_heldout.jsonl (the triage gate's cases), never shown to the proposer.

Environment:
    PROPOSER_MODEL    litellm model id for the proposer and critic (required)
    PROPOSER_PRICES   "in,out" $ per million tokens, if litellm doesn't know the model's price
    TRIAGE_MODEL      the agent's model (default: production's, from config/llm_routing.json)
    N_EVOLVE          evolve cases (default 60)
    ANTHROPIC_API_KEY for the agent
"""
from __future__ import annotations

import json
import os
import random
import sys
from collections import defaultdict
from pathlib import Path

from harness_evolve import AgentProfile, Optimizer, OptimizerConfig, anchors, litellm_llm, profiles
from harness_evolve.command import CommandEvaluator
from harness_evolve.grader import TrajectoryGrader

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HARNESS = ROOT / "app" / "agents" / "harness" / "triage"
TRAIN = ROOT / "app" / "evals" / "triage_train.jsonl"
HELDOUT = ROOT / "app" / "evals" / "triage_heldout.jsonl"

AGENT_DESCRIPTION = (
    "TriageAgent classifies a production error event as real, noise or duplicate and assigns a P0-P3 severity. "
    "It is a ReAct loop on a small model with two tools: check_duplicate_pr (is there an open PR for this "
    "error?) and get_occurrence_count (how many times did it fire in CloudWatch in the last 24h?). It ends "
    "with one JSON object: decision, severity, blast_radius, occurrences_24h, duplicate_pr, reasoning. It "
    "runs on every alert, before diagnosis; noise and duplicate stop the pipeline.")
SAFETY = [
    "calling check_duplicate_pr before deciding",
    "calling get_occurrence_count when the event has a log group, and reporting only the count it returned",
    "deciding 'duplicate' only when check_duplicate_pr returned DUPLICATE",
    "answering with only the JSON object",
]
TASK_CONTRACT = ("The decision and severity match the case's label (or one of its accepted alternatives), from "
                 "the event text and the two tools' results, and the answer is a parseable JSON object.")
COMMAND = [sys.executable, str(HERE / "run_case.py"), "{harness}", "{case}"]
GRADE_COMMAND = [sys.executable, str(HERE / "run_case.py"), "--grade", "{case}", "{output}"]

PROFILE = AgentProfile.build(
    name="triage", harness_root=HARNESS, agent_description=AGENT_DESCRIPTION, safety_mechanisms=SAFETY,
    constraints="Edits must help on any production error event, not on particular error types, services or cases.",
    production_context="Runs on every production alert; one wrong 'noise' or 'duplicate' hides a real bug.",
    gaming_examples="E.g. telling the agent to always answer real/P2, the most common label.",
    setting_bounds={"max_iterations": (4, 15)}, grader=TrajectoryGrader(), task_contract=TASK_CONTRACT)


def _ids(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def evolve_sample(n: int, seed: int = 0) -> list[str]:
    """n training cases, the same share of each decision/severity label as the training set (seeded)."""
    by_label: dict[str, list[str]] = defaultdict(list)
    for c in _ids(TRAIN):
        by_label[f"{c['output']['decision']}/{c['output']['severity']}"].append(c["id"])
    total = sum(map(len, by_label.values()))
    rng, picked = random.Random(seed), []
    for label in sorted(by_label):
        ids = sorted(by_label[label])
        rng.shuffle(ids)
        picked += ids[:max(1, round(n * len(ids) / total))]
    rng.shuffle(picked)
    return picked[:n]


def build(run_dir, budget_usd: float = 5.0, rounds: int = 3) -> Optimizer:
    profiles.use(PROFILE)
    evolve = evolve_sample(int(os.environ.get("N_EVOLVE", "60")))
    held = [c["id"] for c in _ids(HELDOUT)]
    cfg = OptimizerConfig(evolve_cases=evolve, guard_cases=[], final_cases=held, final_trials=2,
                          budget_usd=budget_usd, max_rounds=rounds, trials=2, calibration_trials=2,
                          default_case_cost_usd=0.001,     # measured: $0.0006 per case on Haiku 5.5
                          cost_band=True, parallel_lanes=4,
                          case_lanes={c: c for c in evolve + held}, smoke_cases=evolve[:3],
                          anchors=anchors.load(HERE / "anchors.jsonl"))
    model = os.environ.get("PROPOSER_MODEL")
    if not model:
        raise SystemExit("set PROPOSER_MODEL to a litellm model id (the proposer and critic)")
    prices = os.environ.get("PROPOSER_PRICES")
    llm = litellm_llm(model, prices=tuple(float(x) for x in prices.split(",")) if prices else None)
    evaluator = CommandEvaluator(COMMAND, concurrency=8, cwd=ROOT, grade_command=GRADE_COMMAND)
    return Optimizer(run_dir, HARNESS, cfg, evaluator=evaluator, proposer_llm=llm, critic_llm=llm,
                     critic_patterns=[])
