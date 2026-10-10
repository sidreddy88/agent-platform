"""FixReplayEvaluator: runs the fix agent with a candidate harness on cases with a
saved, localized diagnosis, grades patches in one batch per trial (resolved =
pass), and hands the optimizer trajectories without any answer content."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.harness_optimizer.evaluator import ProviderFailure
from app.harness_optimizer.fix_evaluator import FixReplayEvaluator


def _evaluator(outcomes, graded, seen):
    """outcomes: case -> "patch" | "none" | "error"; graded: case -> resolved."""
    instances = {c: {"instance_id": c, "repo": "o/r"} for c in outcomes}

    async def run_case(inst, diag, harness_dir):
        seen.append((inst["instance_id"], harness_dir, diag["affected_file"]))
        kind = outcomes[inst["instance_id"]]
        if kind == "error":
            return {"fix": {"error": "test sandbox failed to start"}}
        rec = {"fix": {"trajectory_steps": [{"iteration": 0, "name": "apply_edit", "input": {}, "output": "✓"}]}}
        if kind == "patch":
            rec["model_patch"] = "diff --git a/x.py b/x.py\n"
        return rec

    def grade(preds, run_id):
        seen.append(("grade", run_id, sorted(p["instance_id"] for p in preds)))
        return {p["instance_id"]: graded.get(p["instance_id"], False) for p in preds}

    ev = FixReplayEvaluator(saved_diagnoses={c: {"affected_file": "x.py"} for c in outcomes},
                            instances_path=Path("/nonexistent"), run_case=run_case, grade=grade)
    ev._instances = instances
    return ev


@pytest.mark.asyncio
async def test_scores_resolved_and_counts_no_patch_as_escalation():
    seen = []
    ev = _evaluator({"a": "patch", "b": "patch", "c": "none"}, {"a": True}, seen)
    out = await ev.evaluate(Path("/cand"), ["a", "b", "c"], trials=2)
    pc = out.result.per_case
    assert (pc["a"].passes, pc["a"].trials) == (2, 2)
    assert (pc["b"].passes, pc["c"].passes, pc["c"].escalations) == (0, 0, 2)
    assert all(h == "/cand" for c, h, _ in seen if c != "grade")
    grades = [s for s in seen if s[0] == "grade"]
    assert len(grades) == 2 and grades[0][2] == ["a", "b"] and grades[0][1] != grades[1][1]


@pytest.mark.asyncio
async def test_trajectories_carry_no_answer_content():
    ev = _evaluator({"a": "patch", "b": "none"}, {}, [])
    out = await ev.evaluate(Path("/cand"), ["a", "b"], trials=1)
    details = {t["instance_id"]: t["detail"] for t in out.trajectories}
    assert details == {"a": "patch failed the tests", "b": "no patch"}
    assert all(set(t) >= {"steps", "verdict", "trial", "cost"} for t in out.trajectories)


@pytest.mark.asyncio
async def test_case_without_a_localized_diagnosis_is_refused():
    ev = _evaluator({"a": "patch"}, {}, [])
    with pytest.raises(ValueError, match="no localized saved diagnosis"):
        await ev.evaluate(Path("/cand"), ["a", "zzz"], trials=1)


@pytest.mark.asyncio
async def test_infrastructure_error_stops_the_evaluation():
    ev = _evaluator({"a": "error"}, {}, [])
    with pytest.raises(ProviderFailure):
        await ev.evaluate(Path("/cand"), ["a"], trials=1)


@pytest.mark.asyncio
async def test_evaluator_trajectories_feed_the_evidence_builder():
    """The smoke run crashed here: llm_calls held None placeholders, which the
    cost-by-source analysis indexes. Build evidence from the real record shape."""
    from app.harness_optimizer import evidence, profiles

    ev = _evaluator({"a": "patch", "b": "none"}, {"a": True}, [])
    out = await ev.evaluate(Path("/cand"), ["a", "b"], trials=1)
    profiles.use("fix")
    try:
        text = evidence.build(out.result, out.trajectories)
    finally:
        profiles.use("diagnosis")
    assert "Graded 2 fix runs." in text and "Failing trajectory: b" in text


@pytest.mark.asyncio
async def test_provider_failure_inside_the_fix_loop_pauses_instead_of_scoring_no_patch():
    seen = []
    ev = _evaluator({"a": "none"}, {}, seen)
    inner = ev._run_case

    async def run_case(inst, diag, harness_dir):
        rec = await inner(inst, diag, harness_dir)
        rec["fix"]["provider_failure"] = "billing"
        return rec

    ev._run_case = run_case
    with pytest.raises(ProviderFailure, match="billing"):
        await ev.evaluate(Path("/cand"), ["a"], trials=1)


def test_fix_provider_failure_classifier():
    from scripts.eval_swebench_fix import _fix_provider_failure

    class E(Exception):
        def __init__(self, msg, status=None):
            super().__init__(msg)
            self.status_code = status

    assert _fix_provider_failure(None) is None
    assert _fix_provider_failure(E("Credit limit exceeded")) == "billing"
    assert _fix_provider_failure(E("x", status=402)) == "billing"
    assert _fix_provider_failure(E("Invalid API key provided")) == "auth"
    assert _fix_provider_failure(E("x", status=503)) == "provider_outage"
    assert _fix_provider_failure(E("some parse error")) is None


@pytest.mark.asyncio
async def test_progress_is_reported_per_finished_fix_run_not_only_after_grading():
    """fix-r1's calibration: a 25-case chunk ran >14 min with no progress recorded, close
    to the loop's 20-minute stall watchdog. Each finished fix run now counts as progress."""
    ev = _evaluator({"a": "patch", "b": "patch", "c": "none"}, {}, [])
    ticks = []
    ev.on_trial = lambda: ticks.append(1)
    await ev.evaluate(Path("/cand"), ["a", "b", "c"], trials=1)
    assert len(ticks) >= 3 * 2          # once per fix run + once per case after grading
