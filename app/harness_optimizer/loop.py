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
    final     (optional) the original and the final harness on the held-out
              cases, k trials each, and a report against the matched-budget
              rerun baseline (report.py)

Before round 0, an optional smoke stage replays a few cases once and checks
they produced tool calls, verdicts and costs, so a broken setup costs ~$1,
not a night.

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
- **Tripwires** (health.py): every few finished cases, the recent trajectories
  are checked for broken measurements (a tool failing far above normal, $0
  costs, errored replays); a trip pauses the run resumably.
- **Watchdog**: a heartbeat file every minute; no progress for
  `stall_minutes` dumps every stack and exits (resumably). A stopped run's
  process once sat hung for 18 hours; this is the fix.
- **Measured timing**: sessions, phase durations and replay counts in the
  state, so "how long did it run unattended" is read, not reconstructed.
"""
from __future__ import annotations

import asyncio
import faulthandler
import hashlib
import json
import logging
import os
import shutil
import time
from collections import deque
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Protocol

from app.harness_optimizer import candidates, critic, evidence, health, proposer, report
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
    # Budget estimate per case per trial until one is measured. Round 0 on
    # Sonnet 5 with caching measured $0.121; 0.25 leaves room for failing
    # cases, which run all 15 turns.
    default_case_cost_usd: float = 0.25
    default_delta: float = 0.25        # used if calibration isn't possible
    acceptance: dict = field(default_factory=dict)   # AcceptanceConfig overrides
    # Parallelism. Cases in the same lane run one after another; lanes run
    # concurrently, at most `parallel_lanes` at a time. Lanes are per repo:
    # replays of the same repo share one base git clone, and running two at
    # once corrupted a replay ("Local repo not available" mid-diagnosis), which
    # would change agent behaviour, not just slow it down.
    parallel_lanes: int = 1
    case_lanes: dict = field(default_factory=dict)   # case id -> lane (repo)
    # Concurrent sub-lanes per repo lane. >1 needs LocalRepoService's
    # per-clone lock and per-instance worktrees (app/services/repo.py).
    lane_width: int = 1
    smoke_cases: list = field(default_factory=list)  # replayed once before round 0
    final_cases: list = field(default_factory=list)  # held-out; empty = no final phase
    final_trials: int = 3
    health_every: int = 10             # finished cases between tripwire checks
    health_window: int = 20            # most recent cases a check looks at
    stall_minutes: float = 20.0        # watchdog: no progress this long -> exit

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


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        if self.run.original_dir.exists():
            shutil.rmtree(self.run.original_dir)
        shutil.copytree(self.base, self.run.original_dir)
        state = RunState(run_id=self.run.root.name, config=self.cfg.to_json(),
                         phase="smoke" if self.cfg.smoke_cases else "evaluate")
        self.run.save(state)
        return state

    async def run_until_stopped(self) -> RunState:
        state = self.run.load() if self.run.exists() else self._init_state()
        if state.phase != "done":
            state.stop_reason = None     # resuming: the last stop (e.g. a provider failure) is over
        budget = Budget(self.cfg.budget_usd, spent_usd=state.spent_usd)
        self._state = state
        self._last_progress = time.monotonic()
        self._begin_session(state)
        watchdog = asyncio.get_running_loop().create_task(self._watchdog(state))
        try:
            while state.phase != "done":
                t0, label = time.monotonic(), f"r{state.round}:{state.phase}"
                started = _now()
                await self._step(state, budget)
                state.timing.setdefault("phases", []).append(
                    {"phase": label, "start": started, "seconds": round(time.monotonic() - t0, 1)})
                self._progress(state)
                self.run.save(state)
        except health.Tripwire as exc:
            # A pause, like a provider failure: the phase is kept.
            state.stop_reason = f"tripwire, check the run then resume: {exc}"
        except BudgetExceeded as exc:
            # Not done either: the phase is kept, so raising the cap (--budget
            # on resume) continues from here, with finished cases from the cache.
            state.stop_reason = f"budget: {exc}"
        except ProviderFailure as exc:
            # Not done: the phase is kept, so a resume continues from here once
            # the provider problem (credits, keys) is fixed.
            state.stop_reason = f"provider failure, resume after fixing: {exc}"
        finally:
            watchdog.cancel()
        state.spent_usd = budget.spent_usd
        self._end_session(state)
        self.run.save(state)
        return state

    # ---- timing, heartbeat, watchdog ------------------------------------------

    def _begin_session(self, state: RunState) -> None:
        t = state.timing
        t.setdefault("started_at", _now())
        t.setdefault("sessions", []).append({"start": _now(), "pid": os.getpid(), "replays": 0,
                                             "start_round": state.round, "start_phase": state.phase})
        self._session_t0 = time.monotonic()

    def _end_session(self, state: RunState) -> None:
        sess = state.timing["sessions"][-1]
        sess["end"] = _now()
        sess["seconds"] = round(time.monotonic() - self._session_t0, 1)
        sess["ended_because"] = state.stop_reason or ("done" if state.phase == "done" else "interrupted")
        if state.phase == "done":
            state.timing["finished_at"] = sess["end"]
        state.timing["active_seconds"] = round(sum(s.get("seconds", 0) for s in state.timing["sessions"]), 1)

    def _progress(self, state: RunState) -> None:
        self._last_progress = time.monotonic()
        sess = state.timing["sessions"][-1]
        sess["last_seen"] = _now()
        sess["seconds"] = round(time.monotonic() - self._session_t0, 1)

    def _heartbeat(self, state: RunState) -> None:
        idle = time.monotonic() - self._last_progress
        data = {"time": _now(), "pid": os.getpid(), "round": state.round, "phase": state.phase,
                "spent_usd": round(state.spent_usd, 4), "seconds_since_progress": round(idle, 1),
                "session_seconds": round(time.monotonic() - self._session_t0, 1)}
        (self.run.root / "heartbeat.json").write_text(json.dumps(data))

    async def _watchdog(self, state: RunState, interval: float = 60.0) -> None:
        while True:
            await asyncio.sleep(interval)
            self._heartbeat(state)
            idle = time.monotonic() - self._last_progress
            if idle > self.cfg.stall_minutes * 60:
                self._die_stalled(state, idle)

    def _die_stalled(self, state: RunState, idle: float) -> None:
        """No progress for stall_minutes: record why, dump every stack, and
        exit hard. A hang may hold threads or subprocess pipes that a normal
        shutdown would wait on forever, which is how r3's process sat for 18h."""
        path = self.run.root / f"watchdog-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.txt"
        with path.open("w") as f:
            f.write(f"no progress for {idle / 60:.1f} min at round {state.round} {state.phase}\n\n")
            faulthandler.dump_traceback(file=f, all_threads=True)
            f.write("\nasyncio tasks:\n")
            for task in asyncio.all_tasks():
                f.write(f"\n{task!r}\n")
                for frame in task.get_stack(limit=12):
                    f.write(f"  {frame.f_code.co_filename}:{frame.f_lineno} {frame.f_code.co_name}\n")
        state.stop_reason = f"watchdog: no progress for {idle / 60:.0f} min, stacks in {path.name}; resume to continue"
        self._end_session(state)
        self.run.save(state)
        os._exit(3)

    async def _step(self, state: RunState, budget: Budget) -> None:
        if state.phase == "smoke":
            await self._smoke(state, budget)
        elif state.phase == "final":
            await self._final(state, budget)
        elif state.round == 0:
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
        why = None
        if state.round > self.cfg.max_rounds:
            why = f"completed {self.cfg.max_rounds} rounds"
        elif state.stalled_rounds >= self.cfg.max_stall:
            why = f"{state.stalled_rounds} consecutive rounds without an acceptance"
        if why is None:
            state.phase = "propose"
        elif self.cfg.final_cases:
            state.phase, state.rounds_stop_reason = "final", why
        else:
            state.phase, state.stop_reason = "done", why

    # ---- evaluation, cached per case ----------------------------------------

    def _case_key(self, harness_dir: Path, trials: int, case: str) -> str:
        return f"{candidates.content_hash(harness_dir)}/k{trials}/{case}"

    def _per_case_estimate(self) -> float:
        """Mean measured cost of one trial of one case so far, for budget reservations.
        Read from disk once, then kept as running sums: rescanning every
        cached case before every case was quadratic, fine at 50 cases but not
        at the final phase's 400."""
        if getattr(self, "_cost_stats", None) is None:
            costs = []
            for p in sorted((self.run.root / "evals").rglob("*.json")):
                case = json.loads(p.read_text())["case"]
                if case["trials"]:
                    costs.append(case["cost_usd"] / case["trials"])
            self._cost_stats = [sum(costs), len(costs)]
        total, n = self._cost_stats
        return total / n if n else self.cfg.default_case_cost_usd

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
                        cases: list[str], trials: int, round0: bool = False) -> tuple[EvalResult, list[dict]]:
        todo = [c for c in cases if self.run.load_eval(self._case_key(harness_dir, trials, c)) is None]
        by_repo: dict[str, list[str]] = {}
        for case in todo:
            by_repo.setdefault(self.cfg.case_lanes.get(case, "_default"), []).append(case)
        # Each repo lane splits into up to lane_width sub-lanes, round-robin.
        lanes: dict[str, list[str]] = {}
        width = max(1, self.cfg.lane_width)
        for repo, repo_cases in by_repo.items():
            for i, case in enumerate(repo_cases):
                lanes.setdefault(f"{repo}#{i % width}", []).append(case)
        self._recent = deque(maxlen=max(1, self.cfg.health_window))
        self._since_check = 0
        self._health_round0 = round0
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
                for kind in (health.Tripwire, BudgetExceeded, ProviderFailure):
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

    def _record_health(self, state: RunState, case: str, record: dict) -> None:
        """Keep the recent finished cases; every health_every of them, run the
        tripwires (health.py) and raise Tripwire if the measurements look broken."""
        recent = getattr(self, "_recent", None)
        if recent is None:
            return
        recent.append((case, record))
        self._since_check += 1
        if self._since_check < self.cfg.health_every:
            return
        self._since_check = 0
        guards = set(self.cfg.guard_cases)
        w = health.Window([], [], [], [])
        for c, rec in recent:
            cr = rec["case"]
            w.trajectories.extend(rec["trajectories"])
            w.verdicts.extend(cr["verdicts"])
            if cr["trials"]:
                w.costs.extend([cr["cost_usd"] / cr["trials"]] * cr["trials"])
            if c in guards:
                w.guard_verdicts.extend(cr["verdicts"])
        problems = health.check(w, round0=self._health_round0)
        h = state.health
        h["checks"] = h.get("checks", 0) + 1
        h["last_check"] = {"time": _now(), "round": state.round, "phase": state.phase,
                           "cases": len(recent), "problems": problems}
        if problems:
            h.setdefault("trips", []).append(h["last_check"])
            self.run.save(state)
            raise health.Tripwire("; ".join(problems))

    async def _reserve(self, budget: Budget, estimate: float, what: str) -> float:
        """Reserve budget for one case, waiting for lanes in flight rather than
        stopping the run. A reservation that fails only because other lanes are
        holding theirs is backpressure; once nothing is in flight, a failure
        means the cap really can't cover the next case, and BudgetExceeded stops
        the run. (The first version raised immediately: with 8 lanes each
        holding an estimate, the 8th lane stopped the whole run at $0 spent.)"""
        if getattr(self, "_released", None) is None:
            self._released = asyncio.Condition()
        async with self._released:
            while True:
                try:
                    return budget.reserve(estimate, what)
                except BudgetExceeded:
                    if budget.reserved_usd <= 0:
                        raise
                    await self._released.wait()

    def _notify_released(self) -> None:
        cond = getattr(self, "_released", None)
        if cond is None:
            return

        async def notify():
            async with cond:
                cond.notify_all()

        asyncio.get_running_loop().create_task(notify())

    async def _evaluate_case(self, state: RunState, budget: Budget, harness_dir: Path,
                             case: str, trials: int) -> None:
        key = self._case_key(harness_dir, trials, case)
        held = await self._reserve(budget, self._per_case_estimate() * trials,
                                   f"round {state.round}: {case} x{trials}")
        try:
            outcome = await self.evaluator.evaluate(harness_dir, [case], trials)
        except ProviderFailure as exc:
            budget.release(held, exc.cost_usd)
            self._notify_released()
            state.spent_usd = budget.spent_usd
            raise
        except BaseException:
            budget.release(held, 0.0)
            self._notify_released()
            raise
        budget.release(held, outcome.cost_usd)
        self._notify_released()
        state.spent_usd = budget.spent_usd
        record = {"case": asdict(outcome.result.per_case[case]), "trajectories": outcome.trajectories}
        self.run.save_eval(key, record)
        stats = getattr(self, "_cost_stats", None)
        if stats is not None and record["case"]["trials"]:
            stats[0] += record["case"]["cost_usd"] / record["case"]["trials"]
            stats[1] += 1
        sess = state.timing.get("sessions")
        if sess:
            sess[-1]["replays"] = sess[-1].get("replays", 0) + record["case"]["trials"]
        self._progress(state)
        self.run.save(state)
        self._record_health(state, case, record)

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
        result, trajs = await self._evaluate(state, budget, self.run.incumbent_dir, cases, k, round0=True)
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

    async def _smoke(self, state: RunState, budget: Budget) -> None:
        """A few cases, one trial each, before any real spend: every replay must
        have made tool calls, reached a verdict and cost money, and the tools
        must not be failing wholesale. Failing it pauses the run for ~$1."""
        cases = list(self.cfg.smoke_cases)
        result, trajs = await self._evaluate(state, budget, self.run.incumbent_dir, cases, 1)
        verdicts = [v for r in result.per_case.values() for v in r.verdicts]
        costs = [r.cost_usd / r.trials for r in result.per_case.values() if r.trials]
        problems = health.smoke_problems(trajs, verdicts, costs)
        state.health["smoke"] = {"time": _now(), "cases": cases, "verdicts": verdicts,
                                 "cost_usd": round(sum(costs), 4), "problems": problems}
        if problems:
            raise health.Tripwire("smoke stage: " + "; ".join(problems))
        state.phase = "evaluate"

    async def _final(self, state: RunState, budget: Budget) -> None:
        """The original and the final harness on the held-out cases, k trials
        each, then the report. If nothing was accepted the two are the same
        bytes, so the content-hash cache runs the original once."""
        cases, k = list(self.cfg.final_cases), self.cfg.final_trials
        orig, _ = await self._evaluate(state, budget, self.run.original_dir, cases, k)
        same = candidates.content_hash(self.run.original_dir) == candidates.content_hash(self.run.incumbent_dir)
        evolved = None
        if not same:
            evolved, _ = await self._evaluate(state, budget, self.run.incumbent_dir, cases, k)
        meta = {"run_id": state.run_id, "heldout_cases": len(cases), "trials": k,
                "accepted": state.accepted, "rounds_stop_reason": state.rounds_stop_reason,
                "original_hash": candidates.content_hash(self.run.original_dir),
                "evolved_hash": candidates.content_hash(self.run.incumbent_dir),
                "spent_usd": round(state.spent_usd, 2)}
        rep_ = report.build(orig.per_case, evolved.per_case if evolved else None, k, meta)
        report.write(self.run.root / "report", rep_)
        state.phase = "done"
        state.stop_reason = f"{state.rounds_stop_reason}; held-out report written ({rep_['verdict'][:160]})"

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
