"""
The harness optimizer: a long-running agent that improves DiagnosisAgent's
harness, one gated edit per round.

    round 0   evaluate the starting harness on the evolve set (2 trials per
              case), calibrate the noise band from trial pairs, set S*
    round r   propose one edit (proposer, from trajectories + edit history)
              -> validate structurally (free)
              -> leakage critic (cheap; rejections go back for repair)
              -> evaluate on the evolve set (the only expensive step)
              -> decide with rules in code (acceptance.py)
              -> record in the edit history; if accepted, the candidate
                 becomes the incumbent and a proposal is published for
                 human review (never applied to the repo automatically)
    stop      max rounds, too many rounds without an acceptance, or the
              budget can't cover the next evaluation

What makes it a long-running agent rather than a script:

- **Checkpointed**: RunState is saved after every phase; `run()` on an existing
  run directory resumes exactly where it stopped.
- **Never pays twice**: each case's result is cached the moment it finishes,
  keyed by the candidate's content hash, so a crash mid-evaluation costs at
  most the case in flight.
- **Memory**: the edit history (history.py) persists every judged candidate
  and is compacted into what the proposer sees each round.
- **Budget**: a hard cap (budget.py) is checked before every case, in code.
- **Human in the loop, asynchronously**: accepted edits become proposals to
  review; the run keeps going without waiting.
- **Stops on provider failures** instead of scoring them, and can be resumed
  once credits or keys are fixed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Protocol

from app.harness_optimizer import candidates, critic, evidence, proposer
from app.harness_optimizer.acceptance import (
    AcceptanceConfig,
    CaseResult,
    EvalResult,
    calibrate_delta,
    decide,
)
from app.harness_optimizer.budget import Budget, BudgetExceeded
from app.harness_optimizer.evaluator import Evaluator, ProviderFailure, UnpricedModel
from app.harness_optimizer.history import EditHistory, HistoryEntry
from app.harness_optimizer.state import RunDir, RunState

logger = logging.getLogger(__name__)

LLM = Callable[[str, str], Awaitable[str]]


@dataclass
class OptimizerConfig:
    evolve_cases: list[str]
    guard_cases: list[str]
    budget_usd: float
    trials: int = 1                    # per case, per candidate evaluation
    calibration_trials: int = 2        # round 0; 2 gives the trial pairs for delta
    max_rounds: int = 5
    max_stall: int = 3                 # consecutive rounds without an acceptance
    repair_attempts: int = 2           # proposer retries after invalid/critic-rejected
    default_case_cost_usd: float = 2.0  # budget estimate until a case is measured
    default_delta: float = 0.25        # used if calibration isn't possible
    acceptance: dict = field(default_factory=dict)   # AcceptanceConfig overrides
    # Parallelism. Cases in the same lane run one after another; lanes run
    # concurrently, at most `parallel_lanes` at a time. Lanes are per repo:
    # replays of the same repo share one base git clone, and running two at
    # once corrupted a replay ("Local repo not available" mid-diagnosis), which
    # would change agent behaviour, not just slow it down.
    parallel_lanes: int = 1
    case_lanes: dict = field(default_factory=dict)   # case id -> lane (repo)

    def to_json(self) -> dict:
        return asdict(self)


class Publisher(Protocol):
    def publish(self, run: RunDir, round_no: int, candidate_dir: Path, diff: str,
                decision: dict, hypothesis: str) -> Path: ...


class LocalPublisher:
    """Writes an accepted edit as a reviewable proposal under run_dir/proposals/.
    Opening the PR is a separate, human-approved step."""

    def publish(self, run: RunDir, round_no: int, candidate_dir: Path, diff: str,
                decision: dict, hypothesis: str) -> Path:
        out = run.root / "proposals" / f"r{round_no}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "harness.diff").write_text(diff)
        (out / "decision.json").write_text(json.dumps(decision, indent=1))
        (out / "README.md").write_text(
            f"# Proposed harness edit, round {round_no}\n\n**Hypothesis:** {hypothesis}\n\n"
            f"**Decision:** {decision['reason']}\n\n"
            f"Improved: {decision['improved'] or '-'}  \nRegressed: {decision['regressed'] or '-'}\n\n"
            f"Apply `harness.diff` to `app/agents/harness/diagnosis/` and open a PR; the "
            f"PR's gate run is the held-out-independent check.\n")
        return out


def _cases_key(cases: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(cases)).encode()).hexdigest()[:10]


class Optimizer:
    def __init__(self, run_dir: str | Path, base_harness: str | Path, cfg: OptimizerConfig,
                 evaluator: Evaluator, proposer_llm: LLM, critic_llm: LLM,
                 critic_patterns: list[tuple[str, str]], publisher: Publisher | None = None):
        self.run = RunDir(run_dir)
        self.base = Path(base_harness)
        self.cfg = cfg
        self.evaluator = evaluator
        self.proposer_llm = proposer_llm
        self.critic_llm = critic_llm
        self.patterns = critic_patterns
        self.publisher = publisher or LocalPublisher()
        self.history = EditHistory(self.run.root / "history.jsonl")

    # ---- lifecycle ----------------------------------------------------------

    def _init_state(self) -> RunState:
        self.run.root.mkdir(parents=True, exist_ok=True)
        if self.run.incumbent_dir.exists():
            shutil.rmtree(self.run.incumbent_dir)
        shutil.copytree(self.base, self.run.incumbent_dir)
        state = RunState(run_id=self.run.root.name, config=self.cfg.to_json())
        self.run.save(state)
        return state

    async def run_until_stopped(self) -> RunState:
        state = self.run.load() if self.run.exists() else self._init_state()
        budget = Budget(self.cfg.budget_usd, spent_usd=state.spent_usd)
        try:
            while state.phase != "done":
                await self._step(state, budget)
                self.run.save(state)
        except BudgetExceeded as exc:
            state.phase, state.stop_reason = "done", f"budget: {exc}"
        except ProviderFailure as exc:
            # Not done: the phase is kept, so a resume continues from here once
            # the provider problem (credits, keys) is fixed.
            state.stop_reason = f"provider failure, resume after fixing: {exc}"
        state.spent_usd = budget.spent_usd
        self.run.save(state)
        return state

    async def _step(self, state: RunState, budget: Budget) -> None:
        if state.round == 0:
            await self._round0(state, budget)
        elif state.phase == "propose":
            await self._propose(state, budget)
        elif state.phase == "screen":
            await self._screen(state, budget)
        elif state.phase == "evaluate":
            await self._evaluate_candidate(state, budget)
        elif state.phase == "decide":
            self._decide(state)
        else:
            raise RuntimeError(f"unknown phase {state.phase!r}")

    def _end_round(self, state: RunState, accepted: bool) -> None:
        state.stalled_rounds = 0 if accepted else state.stalled_rounds + 1
        state.candidate = None
        state.round += 1
        if state.round > self.cfg.max_rounds:
            state.phase, state.stop_reason = "done", f"completed {self.cfg.max_rounds} rounds"
        elif state.stalled_rounds >= self.cfg.max_stall:
            state.phase, state.stop_reason = "done", (
                f"{state.stalled_rounds} consecutive rounds without an acceptance")
        else:
            state.phase = "propose"

    # ---- evaluation, cached per case ----------------------------------------

    def _case_key(self, harness_dir: Path, trials: int, case: str) -> str:
        return f"{candidates.content_hash(harness_dir)}/k{trials}/{case}"

    def _per_case_estimate(self) -> float:
        """Mean measured cost of one trial of one case so far, for budget reservations."""
        costs = []
        for p in sorted((self.run.root / "evals").rglob("*.json")):
            case = json.loads(p.read_text())["case"]
            if case["trials"]:
                costs.append(case["cost_usd"] / case["trials"])
        return sum(costs) / len(costs) if costs else self.cfg.default_case_cost_usd

    async def _llm_call(self, llm: LLM, budget: Budget, state: RunState, system: str, prompt: str) -> str:
        """Proposer/critic calls count against the same budget as evaluations."""
        from app.services import cost_meter

        with cost_meter.metered() as m:
            try:
                return await llm(system, prompt)
            finally:
                summary = m.summary()
                budget.record(summary["cost_usd"] or 0.0)
                state.spent_usd = budget.spent_usd
                if summary["unpriced_models"]:
                    raise UnpricedModel(f"no price for {summary['unpriced_models']}; "
                                        f"add it to cost_meter.PRICES_PER_MTOK")

    async def _evaluate(self, state: RunState, budget: Budget, harness_dir: Path,
                        cases: list[str], trials: int) -> tuple[EvalResult, list[dict]]:
        import asyncio

        todo = [c for c in cases if self.run.load_eval(self._case_key(harness_dir, trials, c)) is None]
        lanes: dict[str, list[str]] = {}
        for case in todo:
            lanes.setdefault(self.cfg.case_lanes.get(case, "_default"), []).append(case)
        gate = asyncio.Semaphore(max(1, self.cfg.parallel_lanes))

        async def run_lane(lane_cases: list[str]) -> None:
            async with gate:
                for case in lane_cases:
                    await self._evaluate_case(state, budget, harness_dir, case, trials)

        if len(lanes) <= 1 or self.cfg.parallel_lanes <= 1:
            for lane_cases in lanes.values():
                await run_lane(lane_cases)
        else:
            # TaskGroup: the first lane to fail (budget, provider failure, crash)
            # cancels the others, so nothing keeps spending after a stop.
            try:
                async with asyncio.TaskGroup() as tg:
                    for lane_cases in lanes.values():
                        tg.create_task(run_lane(lane_cases))
            except BaseExceptionGroup as group:
                # Surface one real cause, so run_until_stopped's budget and
                # provider-failure handling sees it; prefer those over others.
                excs = list(group.exceptions)
                for kind in (BudgetExceeded, ProviderFailure):
                    for exc in excs:
                        if isinstance(exc, kind):
                            raise exc from None
                raise excs[0] from None

        per_case: dict[str, CaseResult] = {}
        trajectories: list[dict] = []
        for case in cases:
            cached = self.run.load_eval(self._case_key(harness_dir, trials, case))
            per_case[case] = CaseResult(**cached["case"])
            trajectories.extend(cached["trajectories"])
        return EvalResult(per_case), trajectories

    async def _evaluate_case(self, state: RunState, budget: Budget, harness_dir: Path,
                             case: str, trials: int) -> None:
        key = self._case_key(harness_dir, trials, case)
        held = budget.reserve(self._per_case_estimate() * trials, f"round {state.round}: {case} x{trials}")
        try:
            outcome = await self.evaluator.evaluate(harness_dir, [case], trials)
        except ProviderFailure as exc:
            budget.release(held, exc.cost_usd)
            state.spent_usd = budget.spent_usd
            raise
        except BaseException:
            budget.release(held, 0.0)
            raise
        budget.release(held, outcome.cost_usd)
        state.spent_usd = budget.spent_usd
        self.run.save_eval(key, {"case": asdict(outcome.result.per_case[case]),
                                 "trajectories": outcome.trajectories})
        self.run.save(state)

    def _eval_ref(self, harness_dir: Path, trials: int) -> dict:
        return {"hash": candidates.content_hash(harness_dir), "trials": trials,
                "cases": self.cfg.evolve_cases + self.cfg.guard_cases}

    def _load_ref(self, ref: dict) -> tuple[EvalResult, list[dict]]:
        per_case, trajs = {}, []
        for case in ref["cases"]:
            data = self.run.load_eval(f"{ref['hash']}/k{ref['trials']}/{case}")
            per_case[case] = CaseResult(**data["case"])
            trajs.extend(data["trajectories"])
        return EvalResult(per_case), trajs

    # ---- phases ---------------------------------------------------------------

    async def _round0(self, state: RunState, budget: Budget) -> None:
        cases = self.cfg.evolve_cases + self.cfg.guard_cases
        k = self.cfg.calibration_trials
        result, trajs = await self._evaluate(state, budget, self.run.incumbent_dir, cases, k)
        pairs = {c: (r.verdicts[0] == "PASS", r.verdicts[1] == "PASS")
                 for c, r in result.per_case.items() if len(r.verdicts) >= 2}
        state.delta = (calibrate_delta(pairs, trials_per_eval=self.cfg.trials)
                       if len(pairs) >= 2 else self.cfg.default_delta)
        # The escalation guard gets its own noise band, from the same trial
        # pairs: did each trial ever reach an accepted submit_diagnosis? A
        # fixed 5pp was 2 trials in 34 on rounds 1-2, close to pure noise.
        from app.harness_optimizer import grader
        never: dict[str, dict[int, bool]] = {}
        for t in trajs:
            never.setdefault(t["instance_id"], {})[t.get("trial")] = not grader.grade(t).accepted
        esc_pairs = {c: (tr[1], tr[2]) for c, tr in never.items() if 1 in tr and 2 in tr}
        state.delta_esc = (calibrate_delta(esc_pairs, trials_per_eval=self.cfg.trials)
                           if len(esc_pairs) >= 2 else None)
        state.S_star = result.S
        state.incumbent_eval = json.dumps(self._eval_ref(self.run.incumbent_dir, k))
        logger.info("round 0: S=%.3f C=$%.3f delta=%.3f", result.S, result.C, state.delta)
        state.round, state.phase = 1, "propose"
        if self.cfg.max_rounds < 1:          # baseline only: measure, propose nothing
            state.phase, state.stop_reason = "done", "round 0 only (baseline and calibration)"

    async def _propose(self, state: RunState, budget: Budget) -> None:
        inc_result, inc_traj = self._load_ref(json.loads(state.incumbent_eval))
        cand = state.candidate or {"id": f"r{state.round}", "repairs": 0, "feedback": ""}
        cand_dir = self.run.candidate_dir(state.round)
        try:
            prop, files = await proposer.propose(
                self.run.incumbent_dir, evidence.build(inc_result, inc_traj),
                self.history.summary(),
                lambda sys_, prompt: self._llm_call(self.proposer_llm, budget, state, sys_, prompt),
                cand.get("feedback", ""))
            candidates.apply_edit(self.run.incumbent_dir, cand_dir, files)
            candidates.validate(self.run.incumbent_dir, cand_dir)
        except candidates.InvalidCandidate as exc:
            if cand["repairs"] < self.cfg.repair_attempts:
                cand["repairs"] += 1
                cand["feedback"] = f"Invalid edit: {exc}"
                state.candidate = cand
                return                                   # retry propose
            self.history.append(HistoryEntry(state.round, cand["id"], "unknown", "", "",
                                             "invalid", str(exc)))
            self._end_round(state, accepted=False)
            return
        cand.update({"component": prop.component, "hypothesis": prop.hypothesis,
                     "dir": str(cand_dir), "hash": candidates.content_hash(cand_dir)})
        state.candidate = cand
        state.phase = "screen"

    async def _screen(self, state: RunState, budget: Budget) -> None:
        cand = state.candidate
        diff = candidates.diff(self.run.incumbent_dir, Path(cand["dir"]))
        rev = await critic.review(
            diff, cand["component"], cand["hypothesis"],
            lambda sys_, prompt: self._llm_call(self.critic_llm, budget, state, sys_, prompt),
            self.patterns)
        if rev.accept:
            state.phase = "evaluate"
            return
        objections = "; ".join(rev.reasons)
        if cand["repairs"] < self.cfg.repair_attempts:
            cand["repairs"] += 1
            cand["feedback"] = f"The leakage critic rejected it: {objections}"
            state.phase = "propose"
            return
        self.history.append(HistoryEntry(state.round, cand["id"], cand["component"],
                                         cand["hypothesis"], diff, "critic_rejected", objections))
        self._end_round(state, accepted=False)

    async def _evaluate_candidate(self, state: RunState, budget: Budget) -> None:
        cand = state.candidate
        await self._evaluate(state, budget, Path(cand["dir"]),
                             self.cfg.evolve_cases + self.cfg.guard_cases, self.cfg.trials)
        cand["eval"] = json.dumps(self._eval_ref(Path(cand["dir"]), self.cfg.trials))
        state.phase = "decide"

    def _decide(self, state: RunState) -> None:
        cand = state.candidate
        inc_result, _ = self._load_ref(json.loads(state.incumbent_eval))
        cand_result, _ = self._load_ref(json.loads(cand["eval"]))
        base = AcceptanceConfig(**self.cfg.acceptance)
        acfg = replace(base, delta=state.delta, guard_cases=tuple(self.cfg.guard_cases),
                       max_escalation_rise=max(base.max_escalation_rise, state.delta_esc or 0.0))
        d = decide(inc_result, cand_result, state.S_star, acfg)
        cand_dir = Path(cand["dir"])
        diff = candidates.diff(self.run.incumbent_dir, cand_dir)
        spent = sum(r.cost_usd for r in cand_result.per_case.values())
        self.history.append(HistoryEntry(
            state.round, cand["id"], cand["component"], cand["hypothesis"], diff,
            "accepted" if d.accept else "rejected", d.reason, d.delta_S, d.delta_C,
            d.improved, d.regressed, round(spent, 4)))
        if d.accept:
            self.publisher.publish(self.run, state.round, cand_dir, diff, d.to_json(), cand["hypothesis"])
            shutil.rmtree(self.run.incumbent_dir)
            shutil.copytree(cand_dir, self.run.incumbent_dir)
            state.incumbent_eval = cand["eval"]
            state.S_star = max(state.S_star or 0.0, cand_result.S)
            state.accepted.append(cand["id"])
        self._end_round(state, accepted=d.accept)
