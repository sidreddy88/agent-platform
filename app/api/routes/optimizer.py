"""
Harness optimizer status, read-only, for the dashboard's Optimizer page.

GET /optimizer/runs              every run under runs/harness/, newest first
GET /optimizer/runs/{run_id}     one run: state, timing, tripwires, the
                                 evaluation in flight, every judged candidate,
                                 and the held-out report once written

Reads the files the optimizer writes (state.json, heartbeat.json, evals/,
history.jsonl, report/); never writes, never starts or stops anything. The
runs live on the machine that ran them (runs/ is gitignored), so on the
deployed server this lists nothing.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/optimizer", tags=["optimizer"])

ROOT = Path(__file__).resolve().parents[3]
RUNS = ROOT / "runs" / "harness"
SPLIT = ROOT / "app" / "evals" / "harness_split.json"
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")

# Case records are ~35KB each and a run has hundreds; keep only the small
# "case" part, re-read when the file changes.
_case_cache: dict[str, tuple[float, dict]] = {}


def _run_dir(run_id: str) -> Path:
    if not _RUN_ID.match(run_id):
        raise HTTPException(400, "bad run id")
    d = (RUNS / run_id).resolve()
    if d.parent != RUNS.resolve() or not (d / "state.json").exists():
        raise HTTPException(404, f"no run {run_id}")
    return d


def _read_json(p: Path) -> Any:
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _case(p: Path) -> dict | None:
    try:
        mtime = p.stat().st_mtime
    except OSError:
        return None
    hit = _case_cache.get(str(p))
    if hit and hit[0] == mtime:
        return hit[1]
    data = _read_json(p)
    if not data:
        return None
    _case_cache[str(p)] = (mtime, data["case"])
    return data["case"]


def _running(d: Path, state: dict) -> tuple[bool, float | None]:
    """Running if the heartbeat is fresh (written every 60s). Runs from before
    the heartbeat existed fall back to how recently state.json changed."""
    hb = _read_json(d / "heartbeat.json")
    if hb:
        age = time.time() - (d / "heartbeat.json").stat().st_mtime
        return state.get("phase") != "done" and age < 150, age
    age = time.time() - (d / "state.json").stat().st_mtime
    return state.get("phase") != "done" and age < 600, None


def _summarize(cases: dict[str, dict]) -> dict:
    trials = sum(c["trials"] for c in cases.values())
    return {
        "cases": len(cases),
        "trials": trials,
        "S": sum(c["passes"] / c["trials"] for c in cases.values() if c["trials"]) / len(cases) if cases else None,
        "C": sum(c["cost_usd"] for c in cases.values()) / trials if trials else None,
        "escalation_rate": sum(c.get("escalations", 0) for c in cases.values()) / trials if trials else None,
        "cost_usd": round(sum(c["cost_usd"] for c in cases.values()), 4),
    }


def _load_eval(d: Path, h: str, k: int, cases: list[str]) -> dict[str, dict]:
    out = {}
    for c in cases:
        rec = _case(d / "evals" / h / f"k{k}" / f"{c}.json")
        if rec:
            out[c] = rec
    return out


def _hash(p: Path) -> str | None:
    if not p.exists():
        return None
    from app.harness_optimizer.candidates import content_hash
    return content_hash(p)


def _in_flight(d: Path, state: dict) -> dict | None:
    """Which evaluation is running now, how far along, and how it compares so
    far with the incumbent on the same cases."""
    cfg, phase = state["config"], state["phase"]
    evolve = cfg["evolve_cases"] + cfg["guard_cases"]
    if phase == "smoke":
        label, h, k, cases = "smoke stage", _hash(d / "incumbent"), 1, cfg.get("smoke_cases", [])
    elif state["round"] == 0 and phase == "evaluate":
        label, h, k, cases = "round 0 baseline", _hash(d / "incumbent"), cfg["calibration_trials"], evolve
    elif phase == "evaluate" and state.get("candidate"):
        label, h, k, cases = f"round {state['round']} candidate", state["candidate"].get("hash"), cfg["trials"], evolve
    elif phase == "final":
        k, cases = cfg.get("final_trials", 3), cfg.get("final_cases", [])
        orig, inc = _hash(d / "original"), _hash(d / "incumbent")
        done_orig = _load_eval(d, orig, k, cases) if orig else {}
        if len(done_orig) < len(cases) or orig == inc:
            label, h = "held-out: original harness", orig
        else:
            label, h = "held-out: evolved harness", inc
    else:
        return None
    if not h:
        return None
    done = _load_eval(d, h, k, cases)
    out = {"label": label, "trials": k, "total": len(cases), "done": len(done), "so_far": _summarize(done)}
    ref = json.loads(state["incumbent_eval"]) if state.get("incumbent_eval") else None
    if ref and phase == "evaluate" and state["round"] > 0 and done:
        inc = _load_eval(d, ref["hash"], ref["trials"], list(done))
        out["incumbent_same_cases"] = _summarize(inc)
    return out


def _tiers(d: Path, state: dict) -> list[dict] | None:
    """The incumbent (round 0 baseline, or the last accepted candidate) per tier."""
    ref = json.loads(state["incumbent_eval"]) if state.get("incumbent_eval") else None
    split = _read_json(SPLIT)
    if not ref or not split:
        return None
    ev = _load_eval(d, ref["hash"], ref["trials"], ref["cases"])
    tiers = [("failing", split["evolve"]["failing"]), ("guards", split["evolve"]["guards"]),
             ("hard", split["evolve"].get("hard", []))]
    return [{"tier": name, **_summarize({c: ev[c] for c in ids if c in ev})} for name, ids in tiers]


def _history(d: Path) -> list[dict]:
    p = d / "history.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _log_tail(d: Path, n: int = 15) -> list[str]:
    p = d.parent / f"{d.name}.log"
    if not p.exists():
        return []
    lines = [l for l in p.read_text(errors="replace").splitlines()[-400:]
             if l.strip() and "log_group missing" not in l and "LiteLLM.Info" not in l]
    return lines[-n:]


def _brief(d: Path) -> dict:
    s = _read_json(d / "state.json") or {}
    running, _ = _running(d, s)
    return {"run_id": d.name, "round": s.get("round"), "phase": s.get("phase"),
            "spent_usd": s.get("spent_usd"), "budget_usd": (s.get("config") or {}).get("budget_usd"),
            "S_star": s.get("S_star"), "accepted": s.get("accepted", []), "stop_reason": s.get("stop_reason"),
            "running": running, "updated_at": (d / "state.json").stat().st_mtime}


@router.get("/runs")
def list_runs() -> list[dict]:
    if not RUNS.exists():
        return []
    runs = [_brief(d) for d in RUNS.iterdir() if (d / "state.json").exists()]
    return sorted(runs, key=lambda r: r["updated_at"], reverse=True)


@router.get("/runs/{run_id}")
def run_detail(run_id: str) -> dict:
    d = _run_dir(run_id)
    s = _read_json(d / "state.json")
    running, hb_age = _running(d, s)
    cfg = s["config"]
    timing = s.get("timing") or {}
    sessions = [dict(x) for x in timing.get("sessions", [])]
    # The optimizer updates a session's seconds when a case finishes; while
    # it runs, count to now so the page's clock doesn't sit still between cases.
    if running and sessions and not sessions[-1].get("end"):
        start = datetime.fromisoformat(sessions[-1]["start"])
        sessions[-1]["seconds"] = round((datetime.now(timezone.utc) - start).total_seconds(), 1)
    elapsed = sum(x.get("seconds", 0) for x in sessions)
    return {
        **_brief(d),
        "heartbeat": _read_json(d / "heartbeat.json"),
        "heartbeat_age_s": hb_age,
        "delta": s.get("delta"), "delta_esc": s.get("delta_esc"),
        "rounds_stop_reason": s.get("rounds_stop_reason"),
        "config": {k: cfg.get(k) for k in ("budget_usd", "trials", "calibration_trials", "max_rounds",
                                             "max_stall", "parallel_lanes", "lane_width", "final_trials")}
                  | {"evolve_cases": len(cfg["evolve_cases"]), "guard_cases": len(cfg["guard_cases"]),
                     "final_cases": len(cfg.get("final_cases", [])), "smoke_cases": len(cfg.get("smoke_cases", []))},
        "timing": {"started_at": timing.get("started_at"), "finished_at": timing.get("finished_at"),
                   "sessions": sessions, "active_hours": round(elapsed / 3600, 2),
                   "phases": timing.get("phases", [])[-40:]},
        "health": s.get("health") or {},
        "candidate": s.get("candidate"),
        "in_flight": _in_flight(d, s),
        "incumbent_tiers": _tiers(d, s) if s.get("round", 0) > 0 or s.get("phase") == "done" else None,
        "history": _history(d),
        "report": _read_json(d / "report" / "report.json"),
        "log_tail": _log_tail(d),
    }
