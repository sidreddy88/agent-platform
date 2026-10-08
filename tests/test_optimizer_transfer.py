"""Two-model acceptance: a candidate the primary model accepts is re-run on the
transfer cases with a second model and vetoed if it resolves more than
transfer_margin fewer than the incumbent there."""
from __future__ import annotations

import asyncio
from pathlib import Path

from app.harness_optimizer.acceptance import CaseResult, EvalResult
from app.harness_optimizer.evaluator import EvalOutcome
from app.harness_optimizer.loop import Optimizer, OptimizerConfig
from tests.test_harness_optimizer import BASE, CASES, GUARDS, PATTERNS, FakeEvaluator, accept_all, terse_proposer


class SecondModel:
    """Resolves `n_inc` transfer cases with the incumbent harness and `n_cand` with a TERSE one."""

    def __init__(self, n_inc: int, n_cand: int):
        self.n_inc, self.n_cand, self.calls = n_inc, n_cand, 0

    async def evaluate(self, harness_dir, case_ids, trials):
        self.calls += 1
        terse = "TERSE" in (Path(harness_dir) / "log_context_missing.prompt").read_text()
        n = self.n_cand if terse else self.n_inc
        per = {c: CaseResult(int(i < n), trials, 0, 0.05, ["PASS" if i < n else "FAIL"])
               for i, c in enumerate(case_ids)}
        return EvalOutcome(EvalResult(per), 0.05 * len(case_ids), [])


def _opt(tmp_path, second, margin=2):
    cfg = OptimizerConfig(evolve_cases=CASES, guard_cases=GUARDS, budget_usd=100.0, max_rounds=1,
                          transfer_cases=[f"t{i}" for i in range(10)], transfer_margin=margin)
    return Optimizer(tmp_path / "run", BASE, cfg, FakeEvaluator(), terse_proposer(), accept_all, PATTERNS,
                     transfer_evaluator=second)


def test_candidate_that_holds_on_the_second_model_is_accepted(tmp_path):
    second = SecondModel(n_inc=6, n_cand=5)                    # drop 1 <= margin 2
    opt = _opt(tmp_path, second)
    state = asyncio.run(opt.run_until_stopped())
    assert state.accepted == ["r1"] and second.calls == 2
    assert "second model: candidate 5 vs incumbent 6" in opt.history.entries()[-1].reason


def test_candidate_that_drops_on_the_second_model_is_vetoed(tmp_path):
    opt = _opt(tmp_path, SecondModel(n_inc=8, n_cand=3))       # drop 5 > margin 2
    state = asyncio.run(opt.run_until_stopped())
    entry = opt.history.entries()[-1]
    assert state.accepted == [] and entry.outcome == "transfer_rejected"
    assert "STEPS 1-3" in (tmp_path / "run" / "incumbent" / "log_context_missing.prompt").read_text()


def test_transfer_check_is_counted_in_the_budget(tmp_path):
    opt = _opt(tmp_path, SecondModel(n_inc=6, n_cand=6))
    state = asyncio.run(opt.run_until_stopped())
    assert abs(state.spent_usd - (4 * 2 * 1.0 + 4 * 1 * 0.6 + 2 * 0.5)) < 1e-6
