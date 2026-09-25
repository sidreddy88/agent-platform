"""Harness optimizer: acceptance rules, candidates, critic, proposer, memory,
budget, and the long-running loop (checkpoint/resume, never paying twice,
provider-failure stop, budget stop). No API calls: evaluator and LLMs are fakes."""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from app.agents.harness import DEFAULT_ROOT
from app.harness_optimizer import candidates, critic, proposer
from app.harness_optimizer.acceptance import (
    AcceptanceConfig,
    CaseResult,
    EvalResult,
    calibrate_delta,
    decide,
)
from app.harness_optimizer.budget import Budget, BudgetExceeded
from app.harness_optimizer.evaluator import EvalOutcome, ProviderFailure
from app.harness_optimizer.history import EditHistory, HistoryEntry
from app.harness_optimizer.loop import Optimizer, OptimizerConfig

BASE = DEFAULT_ROOT / "diagnosis"


def ev(**cases) -> EvalResult:
    """ev(a=(passes, trials, cost[, escalations]))"""
    out = {}
    for name, spec in cases.items():
        p, t, c, *e = spec
        out[name] = CaseResult(p, t, e[0] if e else 0, c, ["PASS"] * p + ["FAIL"] * (t - p))
    return EvalResult(out)


# ---- acceptance -------------------------------------------------------------

def test_in_band_candidate_wins_only_by_being_cheaper():
    inc = ev(a=(1, 2, 2.0), b=(2, 2, 2.0))
    cheaper = ev(a=(1, 2, 1.4), b=(2, 2, 1.4))
    pricier = ev(a=(2, 2, 2.4), b=(2, 2, 2.4))
    cfg = AcceptanceConfig(delta=0.3)
    assert decide(inc, cheaper, inc.S, cfg).accept
    d = decide(inc, pricier, inc.S, cfg)      # +0.25 dS is inside the 0.3 band: no credit
    assert not d.accept and "must be cheaper" in d.reason
    assert d.improved == ["a"]


def test_clear_gain_may_cost_more_within_the_rrsi_budget():
    inc = ev(a=(0, 2, 2.0), b=(0, 2, 2.0))
    better = ev(a=(2, 2, 2.4), b=(2, 2, 2.4))         # dS = +1.0, dC = +20%
    assert decide(inc, better, inc.S, AcceptanceConfig(delta=0.3)).accept


def test_guard_and_escalation_vetoes():
    inc = ev(a=(1, 2, 2.0), g=(2, 2, 1.0))
    broke_guard = ev(a=(1, 2, 1.0), g=(1, 2, 0.5))
    cfg = AcceptanceConfig(delta=0.3, guard_cases=("g",))
    assert "guard case g" in decide(inc, broke_guard, inc.S, cfg).reason
    gives_up = ev(a=(1, 2, 0.5, 2), g=(2, 2, 0.5))    # cheaper by escalating
    assert "escalation rate rose" in decide(inc, gives_up, inc.S, cfg).reason


def test_floor_rejects_a_slide_below_best_so_far():
    inc = ev(a=(1, 2, 2.0), b=(1, 2, 2.0))
    worse_cheaper = ev(a=(0, 2, 0.5), b=(0, 2, 0.5))
    d = decide(inc, worse_cheaper, S_star=0.8, cfg=AcceptanceConfig(delta=0.2))
    assert not d.accept and "noise floor" in d.reason


def test_calibrated_delta_is_zero_for_identical_trials_and_shrinks_with_k():
    same = {f"c{i}": (True, True) for i in range(10)}
    assert calibrate_delta(same) == 0.0
    noisy = {f"c{i}": (i % 2 == 0, i % 3 == 0) for i in range(12)}
    d1, d2 = calibrate_delta(noisy), calibrate_delta(noisy, trials_per_eval=2)
    assert d1 > 0 and d2 == pytest.approx(d1 / 2 ** 0.5)


# ---- candidates -------------------------------------------------------------

def _copy_base(tmp_path: Path) -> Path:
    d = tmp_path / "base"
    shutil.copytree(BASE, d)
    return d


def test_edit_surface_is_bounded_and_validated(tmp_path):
    base = _copy_base(tmp_path)
    with pytest.raises(candidates.InvalidCandidate, match="outside the harness surface"):
        candidates.apply_edit(base, tmp_path / "c", {"new.prompt": "x"})
    missing = (base / "log_context_missing.prompt").read_text()
    with pytest.raises(candidates.InvalidCandidate, match="changes nothing"):
        candidates.apply_edit(base, tmp_path / "c", {"log_context_missing.prompt": missing})

    settings = json.loads((base / "settings.json").read_text())
    for bad, match in [({**settings, "max_iterations": 1}, "outside"),
                       ({**settings, "max_iterations": "15"}, "type")]:
        candidates.apply_edit(base, tmp_path / "c", {"settings.json": json.dumps(bad)})
        with pytest.raises(candidates.InvalidCandidate, match=match):
            candidates.validate(base, tmp_path / "c")

    candidates.apply_edit(base, tmp_path / "c", {"stack_trace.prompt": "{paths} {secret}"})
    with pytest.raises(candidates.InvalidCandidate, match="never fills"):
        candidates.validate(base, tmp_path / "c")

    candidates.apply_edit(base, tmp_path / "c", {"log_context_missing.prompt": "\nNo logs.\n"})
    candidates.validate(base, tmp_path / "c")
    assert candidates.changed_files(base, tmp_path / "c") == ["log_context_missing.prompt"]
    assert candidates.content_hash(base) != candidates.content_hash(tmp_path / "c")
    assert "+No logs." in candidates.diff(base, tmp_path / "c")


# ---- critic -------------------------------------------------------------------

PATTERNS = critic.domain_patterns(["sympy__sympy-12096"], ["sympy/sympy", "pydata/xarray"],
                                  ["lib/matplotlib/colorbar.py"])


def test_precheck_catches_leakage_in_added_lines_only():
    leaky = "+When the issue mentions xarray, open lib/matplotlib/colorbar.py first\n"
    hits = critic.precheck(leaky, PATTERNS)
    assert any("pydata/xarray" in h for h in hits) and any("colorbar" in h for h in hits)
    assert critic.precheck("-old line about sympy\n+generic advice\n", PATTERNS) == []


def test_critic_parses_fenced_json_and_rejects_when_it_cannot():
    async def fenced(system, prompt):
        return '```json\n{"verdict": "accept", "reasons": [], "risk_notes": ["fine"]}\n```'

    async def garbage(system, prompt):
        return "sure!"

    r = asyncio.run(critic.review("+generic advice\n", "task_prompt", "h", fenced, PATTERNS))
    assert r.accept and r.risk_notes == ["fine"]
    r = asyncio.run(critic.review("+generic advice\n", "task_prompt", "h", garbage, PATTERNS))
    assert not r.accept and "no valid verdict" in r.reasons[0]


# ---- proposer -----------------------------------------------------------------

def test_proposer_ops_must_match_exactly_once(tmp_path):
    base = _copy_base(tmp_path)
    ok = proposer.apply_ops(base, [{"file": "stack_trace.prompt", "find": "{paths}",
                                    "replace": "{paths}\n(read these first)"}])
    assert "(read these first)" in ok["stack_trace.prompt"]
    with pytest.raises(candidates.InvalidCandidate, match="occurs 0 times"):
        proposer.apply_ops(base, [{"file": "stack_trace.prompt", "find": "nope", "replace": "x"}])
    with pytest.raises(candidates.InvalidCandidate, match="component"):
        proposer.parse('{"component": "base.py", "hypothesis": "h", "edits": [{"file":"a","find":"b","replace":"c"}]}')


# ---- memory and budget ----------------------------------------------------------

def test_history_summary_keeps_newest_in_full_and_compacts_older(tmp_path):
    h = EditHistory(tmp_path / "history.jsonl")
    for i in range(6):
        h.append(HistoryEntry(i, f"r{i}", "task_prompt", f"hypothesis {i}", "+x\n" * 50,
                              "rejected", f"reason {i}", 0.0, 0.1))
    s = h.summary(recent=2)
    assert "task_prompt: rejected: 6" in s
    assert "r0 [task_prompt] rejected" in s and "--- r5 r5" in s and "--- r1 r1" not in s
    assert len(h.summary(max_chars=500)) <= 500


def test_budget_refuses_before_overspending():
    b = Budget(cap_usd=10.0, spent_usd=7.0)
    held = b.reserve(2.0, "small")                       # 2.5 with margin fits
    b.release(held, actual_usd=2.0)                      # it cost 2.0: 9.0 spent
    with pytest.raises(BudgetExceeded, match="only \\$1.00"):
        b.reserve(3.0, "too big")


# ---- the loop -------------------------------------------------------------------

CASES, GUARDS = ["c1", "c2", "c3"], ["g1"]


class FakeEvaluator:
    """Deterministic by harness content: a harness whose log_context_missing
    prompt says TERSE is 40% cheaper; everything else behaves identically,
    with c1 failing trial 2 (so calibration has something to measure)."""

    def __init__(self, fail_after: int | None = None, provider_fail_at: int | None = None):
        self.calls: list[tuple[str, str]] = []
        self.fail_after, self.provider_fail_at = fail_after, provider_fail_at

    async def evaluate(self, harness_dir, case_ids, trials):
        (case,) = case_ids
        n = len(self.calls) + 1
        if self.fail_after is not None and n > self.fail_after:
            raise RuntimeError("process died")
        if self.provider_fail_at == n:
            raise ProviderFailure("credit balance is too low", cost_usd=0.5)
        self.calls.append((candidates.content_hash(harness_dir), case))
        terse = "TERSE" in (Path(harness_dir) / "log_context_missing.prompt").read_text()
        verdicts = ["PASS", "FAIL"][:trials] if case == "c1" else ["PASS"] * trials
        cost = (0.6 if terse else 1.0) * trials
        cr = CaseResult(verdicts.count("PASS"), trials, 0, cost, verdicts)
        return EvalOutcome(EvalResult({case: cr}), cost, [{"instance_id": case, "verdict": verdicts[0],
                                                            "llm_calls": [], "steps": []}])


def terse_proposer(component="prompt_fragments"):
    async def llm(system, prompt):
        return json.dumps({"component": component, "hypothesis": "shorter log-context note",
                           "edits": [{"file": "log_context_missing.prompt", "find": "STEPS 1-3",
                                      "replace": "TERSE STEPS 1-3"}]})
    return llm


async def accept_all(system, prompt):
    return '{"verdict": "accept", "reasons": []}'


async def reject_all(system, prompt):
    return '{"verdict": "reject", "reasons": ["weakens grounding"]}'


def _opt(tmp_path, evaluator, proposer_llm=None, critic_llm=accept_all, **cfg):
    config = OptimizerConfig(evolve_cases=CASES, guard_cases=GUARDS, budget_usd=cfg.pop("budget", 100.0),
                             max_rounds=cfg.pop("max_rounds", 2), **cfg)
    return Optimizer(tmp_path / "run", BASE, config, evaluator, proposer_llm or terse_proposer(),
                     critic_llm, PATTERNS)


def test_full_run_accepts_a_cheaper_edit_and_publishes_it(tmp_path):
    opt = _opt(tmp_path, FakeEvaluator(), max_rounds=1)
    state = asyncio.run(opt.run_until_stopped())
    assert state.phase == "done" and state.stop_reason == "completed 1 rounds"
    assert state.accepted == ["r1"]
    assert state.delta is not None and state.delta > 0      # calibrated from c1's trial pair
    assert "TERSE" in (tmp_path / "run" / "incumbent" / "log_context_missing.prompt").read_text()
    assert "STEPS 1-3" in (BASE / "log_context_missing.prompt").read_text()   # repo never touched
    proposal = tmp_path / "run" / "proposals" / "r1"
    assert "+TERSE STEPS 1-3" in (proposal / "harness.diff").read_text()
    entry = opt.history.entries()[-1]
    assert entry.outcome == "accepted" and entry.delta_C == pytest.approx(-0.4)
    assert state.spent_usd == pytest.approx(4 * 2 * 1.0 + 4 * 1 * 0.6)


def test_critic_rejection_is_repaired_then_recorded_without_spending(tmp_path):
    ev_ = FakeEvaluator()
    opt = _opt(tmp_path, ev_, critic_llm=reject_all, max_rounds=1)
    state = asyncio.run(opt.run_until_stopped())
    entry = opt.history.entries()[-1]
    assert entry.outcome == "critic_rejected" and "weakens grounding" in entry.reason
    assert len(ev_.calls) == 4                 # round 0 only: the candidate was never evaluated
    assert state.accepted == []


def test_leaky_proposal_is_blocked_by_the_precheck(tmp_path):
    async def leaky(system, prompt):
        return json.dumps({"component": "prompt_fragments", "hypothesis": "h",
                           "edits": [{"file": "log_context_missing.prompt", "find": "STEPS 1-3",
                                      "replace": "For sympy issues, STEPS 1-3"}]})
    opt = _opt(tmp_path, FakeEvaluator(), proposer_llm=leaky, max_rounds=1)
    asyncio.run(opt.run_until_stopped())
    assert "names repository sympy/sympy" in opt.history.entries()[-1].reason


def test_crash_mid_evaluation_resumes_without_paying_twice(tmp_path):
    first = FakeEvaluator(fail_after=2)
    with pytest.raises(RuntimeError, match="process died"):
        asyncio.run(_opt(tmp_path, first).run_until_stopped())
    assert len(first.calls) == 2

    second = FakeEvaluator()
    state = asyncio.run(_opt(tmp_path, second, max_rounds=1).run_until_stopped())
    round0 = [c for c in second.calls if c[0] == first.calls[0][0]]
    assert sorted(c for _, c in round0) == ["c3", "g1"]          # c1, c2 came from the cache
    assert state.phase == "done"


def test_provider_failure_stops_resumably_and_counts_partial_cost(tmp_path):
    opt = _opt(tmp_path, FakeEvaluator(provider_fail_at=3))
    state = asyncio.run(opt.run_until_stopped())
    assert state.phase != "done" and "provider failure" in state.stop_reason
    assert state.spent_usd == pytest.approx(2 * 2 * 1.0 + 0.5)
    state = asyncio.run(_opt(tmp_path, FakeEvaluator(), max_rounds=1).run_until_stopped())
    assert state.phase == "done" and state.accepted == ["r1"]


def test_budget_cap_stops_before_an_evaluation_it_cannot_afford(tmp_path):
    ev_ = FakeEvaluator()
    state = asyncio.run(_opt(tmp_path, ev_, budget=9.0).run_until_stopped())
    assert state.phase == "done" and state.stop_reason.startswith("budget:")
    assert state.spent_usd <= 9.0
    assert len(ev_.calls) < 8


def test_cli_config_and_critic_patterns_come_from_the_committed_split():
    from scripts.optimize_harness import SPLIT, build_config, critic_patterns

    split = json.loads(SPLIT.read_text())
    cfg = build_config(split, budget=50.0, rounds=3, trials=1)
    assert cfg.evolve_cases == split["evolve"]["failing"] + split["evolve"]["hard"]
    assert cfg.guard_cases == split["evolve"]["guards"]
    heldout = split["heldout"]["failing"] + split["heldout"]["stable"] + split["heldout"]["hard"]
    assert not set(cfg.evolve_cases) & set(heldout)
    pats = critic_patterns(split)
    # held-out repos, held-out case ids and true-fix paths are all denylisted
    diff = "+For xarray, look in lib/matplotlib/colorbar.py (see sphinx-doc__sphinx-11445)\n"
    hits = critic.precheck(diff, pats)
    assert any("pydata/xarray" in h for h in hits)
    assert any("sphinx-doc__sphinx-11445" in h for h in hits)
    assert any("colorbar.py" in h for h in hits)


def test_rounds_zero_measures_the_baseline_and_proposes_nothing(tmp_path):
    async def must_not_be_called(system, prompt):
        raise AssertionError("proposer called in a round-0-only run")

    ev_ = FakeEvaluator()
    state = asyncio.run(_opt(tmp_path, ev_, proposer_llm=must_not_be_called, max_rounds=0).run_until_stopped())
    assert state.phase == "done" and state.stop_reason.startswith("round 0 only")
    assert state.delta is not None and state.S_star is not None and len(ev_.calls) == 4


def test_an_unpriced_model_stops_the_run_instead_of_spending_blind(tmp_path):
    from app.harness_optimizer.evaluator import UnpricedModel

    class Blind(FakeEvaluator):
        async def evaluate(self, harness_dir, case_ids, trials):
            raise UnpricedModel("no price for ['claude-mystery-9']")

    state = asyncio.run(_opt(tmp_path, Blind()).run_until_stopped())
    assert state.phase != "done" and "no price" in state.stop_reason


def test_proposer_and_critic_calls_on_an_unpriced_model_also_stop(tmp_path):
    from app.services import cost_meter

    async def unpriced_llm(system, prompt):
        cost_meter.record("claude-mystery-9", 100, 100)
        return '{"verdict": "accept", "reasons": []}'

    opt = _opt(tmp_path, FakeEvaluator(), proposer_llm=terse_proposer(), critic_llm=unpriced_llm, max_rounds=1)
    state = asyncio.run(opt.run_until_stopped())
    assert state.phase == "screen" and "no price" in state.stop_reason


def test_the_routed_diagnosis_model_is_priced():
    from app.services.cost_meter import PRICES_PER_MTOK, _price_key
    from app.services.llm_gateway import llm_gateway

    _, model, _ = llm_gateway._get_routing("diagnosis")
    assert _price_key(model) in PRICES_PER_MTOK, f"routing.diagnosis.model {model} has no price"


class SlowEvaluator(FakeEvaluator):
    """Awaits, so lanes genuinely overlap; records concurrency per lane."""

    def __init__(self, lanes, **kw):
        super().__init__(**kw)
        self.lanes, self.active, self.max_active, self.max_per_lane = lanes, {}, 0, 0

    async def evaluate(self, harness_dir, case_ids, trials):
        lane = self.lanes[case_ids[0]]
        self.active[lane] = self.active.get(lane, 0) + 1
        self.max_active = max(self.max_active, sum(self.active.values()))
        self.max_per_lane = max(self.max_per_lane, self.active[lane])
        try:
            await asyncio.sleep(0.05)
            return await super().evaluate(harness_dir, case_ids, trials)
        finally:
            self.active[lane] -= 1


LANES = {"c1": "repoA", "c2": "repoA", "c3": "repoB", "g1": "repoC"}


def test_parallel_lanes_overlap_repos_but_never_cases_of_one_repo(tmp_path):
    ev_ = SlowEvaluator(LANES)
    state = asyncio.run(_opt(tmp_path, ev_, max_rounds=0, parallel_lanes=3, case_lanes=LANES)
                        .run_until_stopped())
    assert state.phase == "done"
    assert ev_.max_per_lane == 1 and ev_.max_active > 1
    assert state.spent_usd == pytest.approx(4 * 2 * 1.0)


def test_a_failure_in_one_lane_stops_the_run_and_settles_the_budget(tmp_path):
    ev_ = SlowEvaluator(LANES, provider_fail_at=2)
    opt = _opt(tmp_path, ev_, max_rounds=0, parallel_lanes=3, case_lanes=LANES)
    state = asyncio.run(opt.run_until_stopped())
    assert state.phase != "done" and "provider failure" in state.stop_reason
    resumed = asyncio.run(_opt(tmp_path, SlowEvaluator(LANES), max_rounds=0, parallel_lanes=3,
                               case_lanes=LANES).run_until_stopped())
    assert resumed.phase == "done"


def test_in_flight_reservations_count_against_the_cap():
    b = Budget(cap_usd=10.0)
    held = b.reserve(3.0, "lane 1")                 # 3.75 held
    b.reserve(3.0, "lane 2")                        # 7.5 held
    with pytest.raises(BudgetExceeded, match="held by evaluations in flight"):
        b.reserve(3.0, "lane 3")
    b.release(held, actual_usd=1.0)
    assert b.spent_usd == 1.0 and b.reserved_usd == pytest.approx(3.75)


def test_critic_and_proposer_are_told_about_eval_only_empty_tools():
    from app.harness_optimizer import critic as c
    from app.harness_optimizer import proposer as p

    for system in (c.SYSTEM, p.SYSTEM):
        assert "search_similar_incidents" in system and "production" in system


def test_a_finished_run_can_be_extended_with_more_rounds(tmp_path):
    asyncio.run(_opt(tmp_path, FakeEvaluator(), max_rounds=0).run_until_stopped())
    from app.harness_optimizer.state import RunDir

    run = RunDir(tmp_path / "run")
    state = run.load()
    assert state.phase == "done"
    # what the CLI does on `--rounds 1` for an existing run
    state.config["max_rounds"] = 1
    state.phase, state.stop_reason = "propose", None
    run.save(state)
    ev_ = FakeEvaluator()
    final = asyncio.run(_opt(tmp_path, ev_, max_rounds=1).run_until_stopped())
    assert final.accepted == ["r1"]
    assert len(ev_.calls) == 4           # round 0 came from the cache; only the candidate's 4 cases ran


def test_escalation_guard_uses_the_calibrated_noise_band(tmp_path):
    """Round 0 calibrates delta_esc from trial pairs of 'never accepted'; the
    veto threshold is max(fixed limit, delta_esc)."""
    from app.harness_optimizer.acceptance import AcceptanceConfig, decide

    inc = ev(a=(1, 2, 2.0, 1), b=(2, 2, 2.0))
    cand = ev(a=(1, 2, 1.0, 2), b=(2, 2, 1.0))          # +1 escalation of 4 trials, cheaper
    strict = AcceptanceConfig(delta=0.3, max_escalation_rise=0.05)
    noise_aware = AcceptanceConfig(delta=0.3, max_escalation_rise=0.30)
    assert "escalation rate rose" in decide(inc, cand, inc.S, strict).reason
    assert decide(inc, cand, inc.S, noise_aware).accept


def test_round0_calibrates_escalation_band_from_trajectories(tmp_path):
    class WithTrials(FakeEvaluator):
        async def evaluate(self, harness_dir, case_ids, trials):
            out = await super().evaluate(harness_dir, case_ids, trials)
            (case,) = case_ids
            accepted = "Diagnosis accepted."
            out.trajectories = [
                {"instance_id": case, "trial": t + 1, "verdict": v, "llm_calls": [],
                 "steps": [{"name": "submit_diagnosis", "input": "{}", "output": accepted if v == "PASS" else "REJECTED"}]}
                for t, v in enumerate(out.result.per_case[case].verdicts)]
            return out

    state = asyncio.run(_opt(tmp_path, WithTrials(), max_rounds=0).run_until_stopped())
    assert state.delta_esc is not None and state.delta_esc > 0     # c1's trials disagree


def test_proposer_is_steered_to_cost_by_source_without_asking_for_brevity():
    from app.harness_optimizer.proposer import SYSTEM

    assert "Cost by prompt source" in SYSTEM
    assert "Never instruct the agent to be brief" in SYSTEM


def test_lanes_wait_for_budget_instead_of_stopping_the_run(tmp_path):
    """The r3 failure: every lane reserved an estimate at once, the last lane
    couldn't, and the whole run stopped at $0 spent. Now it waits for a lane to
    release its hold. Each case costs 2.0 (1.0 x 2 trials); reservations are
    2.5 with margin, so 3 lanes at once need 7.5 > 6.0, but run in turn they
    fit: total spend is 8.0 under a 9.0 cap."""
    ev_ = SlowEvaluator(LANES)
    state = asyncio.run(_opt(tmp_path, ev_, budget=9.0, max_rounds=0, parallel_lanes=3,
                             case_lanes=LANES, default_case_cost_usd=1.0).run_until_stopped())
    assert state.phase == "done" and state.stop_reason.startswith("round 0 only")
    assert state.spent_usd == pytest.approx(8.0) and len(ev_.calls) == 4


def test_true_budget_exhaustion_still_stops(tmp_path):
    ev_ = SlowEvaluator(LANES)
    state = asyncio.run(_opt(tmp_path, ev_, budget=5.0, max_rounds=0, parallel_lanes=3,
                             case_lanes=LANES, default_case_cost_usd=1.0).run_until_stopped())
    assert state.phase == "done" and state.stop_reason.startswith("budget:")
    assert state.spent_usd <= 5.0
