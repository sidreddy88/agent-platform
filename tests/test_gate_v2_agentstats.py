"""Diagnosis gate on agentstats: gate v2's per-trial results as agentstats rows, and the fail-closed row export."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.evals import gate_v2

ROOT = Path(__file__).resolve().parent.parent


def test_trial_row_maps_verdicts_checks_cost_and_no_answer():
    ok = gate_v2.trial_row("a__a-1", 1, "PASS", "a/a", ["grep_codebase", "submit_diagnosis"], {"cost_usd": 0.04})
    assert ok["passed"] is True and ok["checks"] == {"submitted_via_tool": True} and ok["cost_usd"] == 0.04
    assert ok["tags"] == ["repo:a/a"]
    gave_up = gate_v2.trial_row("a__a-1", 2, "FAIL", "a/a", ["grep_codebase"] * 15)
    assert gave_up["passed"] is False and gave_up["no_answer"] == "no_submission"
    assert gave_up["checks"] == {"submitted_via_tool": False} and gave_up["steps"] == 15.0
    wrong = gate_v2.trial_row("a__a-1", 3, "FAIL", "a/a", ["submit_diagnosis"])
    assert wrong["passed"] is False and "no_answer" not in wrong
    billing = gate_v2.trial_row("a__a-1", 4, "BILLING", "a/a")
    assert billing["passed"] is None and billing["infra_error"] == "BILLING"


def test_collect_rows_reads_trajectories(tmp_path):
    d = tmp_path / "evals" / "h1" / "k2"
    d.mkdir(parents=True)
    (d / "x__x-1.json").write_text(json.dumps({
        "case": {"verdicts": ["PASS", "FAIL"]},
        "trajectories": [{"repo": "x/x", "cost": {"cost_usd": 0.01}, "steps": [{"name": "submit_diagnosis"}]},
                         {"repo": "x/x", "cost": {"cost_usd": 0.02}, "steps": [{"name": "grep_codebase"}]}]}))
    rows = gate_v2.collect_rows([tmp_path], "h1", 2)
    assert [r["run"] for r in rows] == [1, 2] and rows[1]["no_answer"] == "no_submission"
    assert [r["checks"]["submitted_via_tool"] for r in rows] == [True, False]


def test_baseline_rows_cover_the_pool():
    pool = gate_v2.load_pool()
    rows = gate_v2.baseline_rows(gate_v2.load_baseline(), pool)
    assert {r["case"] for r in rows} == set(pool["cases"])
    assert all(r["tags"][0].startswith("repo:") for r in rows)


def test_agentstats_rows_fail_closed_on_a_missing_case(tmp_path):
    pool = gate_v2.load_pool()["cases"]
    shard = tmp_path / "shard.json"
    rows = [gate_v2.trial_row(c, 1, "PASS", "r/r", ["submit_diagnosis"]) for c in pool[1:]]   # drop one case
    shard.write_text(json.dumps({"verdicts": {}, "rows": rows}))
    r = subprocess.run([sys.executable, "scripts/gate_v2.py", "agentstats-rows", "--results", str(shard),
                        "--pr-out", str(tmp_path / "pr.jsonl"), "--baseline-out", str(tmp_path / "b.jsonl")],
                       cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 1 and "failing closed" in r.stdout and not (tmp_path / "pr.jsonl").exists()


def test_the_policy_has_a_full_tier_agentstats_can_read():
    import tomllib

    from agentstats.policy import parse
    policy = parse(tomllib.loads((ROOT / "app/evals/diagnosis_gate_policy.toml").read_text()))
    assert "full" in policy.tiers
