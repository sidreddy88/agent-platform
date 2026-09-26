"""Optimizer status API: read-only views of a run directory."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routes import optimizer


@pytest.fixture
def client(tmp_path, monkeypatch):
    runs = tmp_path / "runs" / "harness"
    run = runs / "r9"
    (run / "evals" / "abc" / "k1").mkdir(parents=True)
    state = {"run_id": "r9", "round": 1, "phase": "evaluate", "spent_usd": 3.5, "S_star": 0.6,
             "delta": 0.1, "delta_esc": 0.1, "accepted": [], "stop_reason": None,
             "candidate": {"id": "r1", "hash": "abc", "component": "task_prompt", "hypothesis": "h"},
             "incumbent_eval": json.dumps({"hash": "inc", "trials": 1, "cases": ["a", "b"]}),
             "config": {"budget_usd": 50, "trials": 1, "calibration_trials": 2, "max_rounds": 3,
                        "max_stall": 3, "parallel_lanes": 2, "evolve_cases": ["a"], "guard_cases": ["b"]},
             "timing": {"started_at": "2026-09-26T00:00:00+00:00",
                        "sessions": [{"start": "2026-09-26T00:00:00+00:00", "seconds": 60, "replays": 1}]},
             "health": {}}
    (run / "state.json").write_text(json.dumps(state))
    (run / "heartbeat.json").write_text("{}")
    (run / "evals" / "abc" / "k1" / "a.json").write_text(json.dumps(
        {"case": {"passes": 1, "trials": 1, "escalations": 0, "cost_usd": 0.2, "verdicts": ["PASS"]},
         "trajectories": []}))
    monkeypatch.setattr(optimizer, "RUNS", runs)
    app = FastAPI()
    app.include_router(optimizer.router)
    return TestClient(app)


def test_lists_runs_and_shows_the_evaluation_in_flight(client):
    runs = client.get("/optimizer/runs").json()
    assert [r["run_id"] for r in runs] == ["r9"] and runs[0]["running"] is True
    d = client.get("/optimizer/runs/r9").json()
    assert d["in_flight"]["label"] == "round 1 candidate"
    assert d["in_flight"]["done"] == 1 and d["in_flight"]["total"] == 2
    assert d["in_flight"]["so_far"]["S"] == 1.0
    assert d["timing"]["sessions"][0]["seconds"] > 60        # live clock while running


def test_rejects_path_traversal_and_unknown_runs(client):
    assert client.get("/optimizer/runs/..%2F..%2Fetc").status_code in (400, 404)
    assert client.get("/optimizer/runs/nope").status_code == 404
