"""
Evaluate a harness directory: run DiagnosisAgent with it on a set of cases.

`Evaluator` is the interface the loop depends on; tests inject a fake.
`ReplayEvaluator` is the real one. It replays SWE-bench cases through the same
path as the regression gate (scripts/eval_diagnosis_full_regression.py's
_replay_attempt: per-case timeout with stack dump, provider-failure
classification) with the agent pointed at the candidate harness, and captures
per-request trajectories and cost.

Trials are independent: unlike the gate, there is no retry-on-failure. The
gate's retry answers "is this case broken?"; here we need unbiased per-case
pass rates for a paired comparison, and trial pairs of the same harness
are what calibrates the noise band.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from app.harness_optimizer.acceptance import CaseResult, EvalResult

ROOT = Path(__file__).resolve().parent.parent.parent
# The 100-instance sample: a superset of the 56-case gate set that also has
# the cases that failed the original baseline (the split's "hard" tier).
SWEBENCH_CASES = ROOT / "app" / "evals" / "swebench_verified_sample.jsonl"
# The rest of the held-out repos' instances from the full 500 (held-out only).
HELDOUT_EXTRA = ROOT / "app" / "evals" / "swebench_heldout_extra.jsonl"


class ProviderFailure(RuntimeError):
    """Auth/billing/rate-limit/outage: the evaluation measured nothing. Stop the
    run rather than score it (the lesson of two credit outages mid-gate-run).
    cost_usd is what the trials that did run cost, so the budget stays honest."""

    def __init__(self, message: str, cost_usd: float = 0.0):
        super().__init__(message)
        self.cost_usd = cost_usd


class UnpricedModel(ProviderFailure):
    """A call came back from a model with no known price, so its cost reads as
    $0 and the budget cap can't see it. Stop (resumably) rather than keep
    spending blind. Found on the first real round 0: evals run on the routed
    diagnosis model (claude-sonnet-5), which the cost meter didn't price, and
    the $80 cap recorded $0 per case."""


@dataclass
class EvalOutcome:
    result: EvalResult
    cost_usd: float
    trajectories: list[dict] = field(default_factory=list)


class Evaluator(Protocol):
    async def evaluate(self, harness_dir: Path, case_ids: list[str], trials: int) -> EvalOutcome: ...


def _is_escalation(verdict: str, detail: str) -> bool:
    return verdict == "FAIL" and "no affected_file" in (detail or "")


class ReplayEvaluator:
    def __init__(self, cases_paths: tuple[Path, ...] = (SWEBENCH_CASES, HELDOUT_EXTRA)):
        self._instances = {}
        for path in cases_paths:
            if path.exists():
                for line in path.read_text().splitlines():
                    if line.strip():
                        inst = json.loads(line)
                        self._instances[inst["instance_id"]] = inst

    async def evaluate(self, harness_dir: Path, case_ids: list[str], trials: int) -> EvalOutcome:
        from app.services import cost_meter
        from app.services.github import GitHubService
        from scripts.eval_diagnosis_full_regression import _replay_attempt
        from scripts.eval_swebench_diagnosis import _replay_one

        github = GitHubService()
        per_case: dict[str, CaseResult] = {}
        trajectories: list[dict] = []
        total = 0.0
        for cid in case_ids:
            inst = self._instances[cid]
            cr = CaseResult(passes=0, trials=0)
            for t in range(trials):
                sink: list[dict] = []

                async def replay(item, gh, _sink=sink):
                    return await _replay_one(item, gh, trajectory_sink=_sink, harness_dir=str(harness_dir))

                with cost_meter.metered() as meter:
                    res = await _replay_attempt(replay, inst, github,
                                                {"instance_id": cid, "repo": inst["repo"]})
                summary = meter.summary()
                if summary["unpriced_models"]:
                    raise UnpricedModel(f"{cid} trial {t + 1}: no price for "
                                        f"{summary['unpriced_models']}; add it to cost_meter.PRICES_PER_MTOK")
                cost = summary["cost_usd"] or 0.0
                total += cost
                if res["verdict"] == "INFRA":
                    raise ProviderFailure(f"{cid} trial {t + 1}: {res['detail'][:300]}", cost_usd=total)
                cr.trials += 1
                cr.passes += res["verdict"] == "PASS"
                cr.escalations += _is_escalation(res["verdict"], res.get("detail", ""))
                cr.cost_usd += cost
                cr.verdicts.append(res["verdict"])
                for rec in sink:
                    trajectories.append({**rec, "trial": t + 1, "verdict": res["verdict"],
                                         "detail": res.get("detail", "")})
            per_case[cid] = cr
        return EvalOutcome(EvalResult(per_case), total, trajectories)
