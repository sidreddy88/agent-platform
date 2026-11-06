"""scripts/triage_evolve_weekly.py: when the weekly job runs the optimizer, and what it proposes. No model calls."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import triage_evolve_weekly as w


def _src(runs, no_answer=0, **checks):
    return {"runs": runs, "no_answer": no_answer, "infra": 0,
            "checks": {name: {"estimate": est, "low": max(0.0, est - 0.2), "high": min(1.0, est + 0.1), "n": runs}
                       for name, est in checks.items()}}


def test_too_few_production_runs_never_triggers_even_with_a_bad_rate():
    d = w.decide({"production / claude-haiku-5-5": _src(8, called_get_occurrence_count=0.5)})
    assert not d["run"] and "only 8" in d["reason"]
    assert d["findings"]                                # still reported, just not acted on


def test_all_rules_holding_does_not_trigger():
    d = w.decide({"production / claude-haiku-5-5": _src(40, called_check_duplicate_pr=1.0,
                                                          called_get_occurrence_count=0.98)})
    assert not d["run"] and "all rules held" in d["reason"]


def test_a_rule_below_the_gate_floor_triggers_with_counts_only():
    d = w.decide({"production / claude-haiku-5-5": _src(40, called_check_duplicate_pr=1.0,
                                                          called_get_occurrence_count=0.6)})
    assert d["run"]
    assert d["findings"] == ["production / claude-haiku-5-5: rule called_get_occurrence_count held in 60% of 40 "
                             "runs (95% CI 40% to 70%; floor 95%)"]


def test_too_many_unusable_answers_triggers():
    d = w.decide({"production / claude-haiku-5-5": _src(40, no_answer=4, called_check_duplicate_pr=1.0)})
    assert d["run"] and "no usable answer in 4 of 40" in d["findings"][0]


def test_eval_and_local_sources_are_ignored():
    d = w.decide({"eval / claude-haiku-5-5": _src(400, called_get_occurrence_count=0.1),
                  "local / claude-haiku-5-5": _src(50, called_get_occurrence_count=0.1)})
    assert not d["run"] and d["runs"] == 0


@pytest.fixture
def run_dir(tmp_path, monkeypatch):
    harness = tmp_path / "harness"
    harness.mkdir()
    (harness / "occurrence_with_log_group.prompt").write_text("old step 2")
    monkeypatch.setattr(w, "HARNESS", harness)
    calls: list = []
    monkeypatch.setattr(w.subprocess, "run", lambda *a, **k: calls.append(a[0]))
    monkeypatch.setattr(w, "_evolve_cli", lambda *a: "--- a/occurrence_with_log_group.prompt\n+++ b/...")
    rd = tmp_path / "run"
    (rd / "incumbent").mkdir(parents=True)
    (rd / "report").mkdir()
    (rd / "incumbent" / "occurrence_with_log_group.prompt").write_text("new step 2")
    (rd / "incumbent" / "README.md").write_text("docs")
    return SimpleNamespace(path=rd, calls=calls, harness=harness)


def _write(rd: Path, accepted, direction="better", report=True):
    rd = rd.path
    (rd / "state.json").write_text(json.dumps({"accepted": accepted, "spent_usd": 4.2, "stop_reason": "done"}))
    if report:
        (rd / "report" / "report.json").write_text(json.dumps({
            "verdict": f"Evolved pass@1 vs original best-of-2: +0.1, {direction}.",
            "comparisons": {"vs_original_single_run": {"diff": 0.1, "ci95": [0.02, 0.18], "direction": direction}}}))
        (rd / "report" / "report.md").write_text("# Held-out report\n| | original | evolved |")


DECISION = {"run": True, "runs": 40, "reason": "1 finding(s) over 40 production runs",
            "findings": ["production / claude-haiku-5-5: rule called_get_occurrence_count held in 60% of 40 runs"]}


def test_an_accepted_edit_that_holds_out_is_applied_with_goldens_and_a_pr_body(run_dir):
    _write(run_dir, ["r2"])
    changed, body = w.apply(run_dir.path, DECISION)
    assert changed
    assert (run_dir.harness / "occurrence_with_log_group.prompt").read_text() == "new step 2"
    assert not (run_dir.harness / "README.md").exists()
    assert any("tests.triage_harness_scenarios" in " ".join(c) for c in run_dir.calls)    # goldens regenerated
    assert "held in 60% of 40 runs" in body and "r2" in body and "$4.20" in body and "draft" in body


@pytest.mark.parametrize("accepted,direction,report", [([], "better", True), (["r1"], "worse", True),
                                                       (["r1"], "better", False)])
def test_nothing_changes_without_an_accepted_edit_that_holds_out(run_dir, accepted, direction, report):
    _write(run_dir, accepted, direction, report)
    changed, body = w.apply(run_dir.path, DECISION)
    assert not changed and body
    assert (run_dir.harness / "occurrence_with_log_group.prompt").read_text() == "old step 2"
    assert run_dir.calls == []


def test_an_edit_already_on_main_is_not_proposed_again(run_dir):
    _write(run_dir, ["r2"])
    (run_dir.harness / "occurrence_with_log_group.prompt").write_text("new step 2")
    changed, body = w.apply(run_dir.path, DECISION)
    assert not changed and "already on main" in body and run_dir.calls == []
