"""Local dashboard for harness-optimizer runs (runs/harness/<run>/).

    python scripts/optimizer_dashboard.py            # then open http://127.0.0.1:8766

Read-only: reads state.json, heartbeat.json, history.jsonl, the per-case eval
cache, proposals/, report/ and the run's .out/.log. Standard library only,
localhost only. Starting or stopping runs stays in the terminal.
"""
from __future__ import annotations

import json
import os
import re
import time
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs" / "harness"
HTML = Path(__file__).with_name("optimizer_dashboard.html")
PORT = 8766
_NOISE = re.compile(r"LiteLLM|completion\(\) model|Give Feedback|_turn_on_debug|^\s*$")


def _json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def runs() -> list[dict]:
    out = []
    for d in RUNS.iterdir() if RUNS.exists() else []:
        st = _json(d / "state.json")
        if not isinstance(st, dict):
            continue
        out.append({"name": d.name, "phase": st.get("phase"), "round": st.get("round"),
                    "spent_usd": round(st.get("spent_usd") or 0, 2),
                    "modified": (d / "state.json").stat().st_mtime})
    return sorted(out, key=lambda r: -r["modified"])


def _cases_dir(run: Path, ref: dict | None) -> Path | None:
    if not ref:
        return None
    return run / "evals" / ref["hash"] / f"k{ref['trials']}"


def _case_table(run: Path, ref: dict | None) -> dict:
    d = _cases_dir(run, ref)
    out = {}
    if d and d.exists():
        for f in d.glob("*.json"):
            c = (_json(f) or {}).get("case") or {}
            out[f.stem] = {"verdicts": c.get("verdicts", []), "cost": round(c.get("cost_usd") or 0, 4)}
    return out


def run_detail(name: str) -> dict:
    run = RUNS / name
    st = _json(run / "state.json", {}) or {}
    cfg = st.get("config") or {}
    hb = _json(run / "heartbeat.json", {}) or {}
    cases = (cfg.get("evolve_cases") or []) + (cfg.get("guard_cases") or [])

    incumbent_ref = json.loads(st["incumbent_eval"]) if st.get("incumbent_eval") else None
    # The evaluation for the current phase: which harness and how many trials. Falls back
    # to the newest eval directory when the phase isn't an evaluation.
    phase, rnd = st.get("phase"), st.get("round")
    label, target = None, None
    try:
        import sys
        sys.path.insert(0, str(ROOT))
        from app.harness_optimizer.candidates import content_hash
        cand_dir = (st.get("candidate") or {}).get("dir")
        if phase == "smoke":
            label, target = "Original harness (smoke check)", (content_hash(run / "original"), 1)
        elif phase == "evaluate" and rnd == 0:
            label, target = "Original harness (calibration)", (content_hash(run / "original"), cfg.get("calibration_trials") or 2)
        elif phase == "evaluate" and cand_dir:
            label, target = "Candidate harness", (content_hash(ROOT / cand_dir if not Path(cand_dir).is_absolute() else Path(cand_dir)), cfg.get("trials") or 1)
    except Exception:
        target = None
    latest, latest_t = None, 0.0
    if target:
        latest = run / "evals" / target[0] / f"k{target[1]}"
        files = list(latest.glob("*.json")) if latest.exists() else []
        latest_t = max((f.stat().st_mtime for f in files), default=0.0)
    else:
        for d in (run / "evals").glob("*/k*") if (run / "evals").exists() else []:
            files = list(d.glob("*.json"))
            t = max((f.stat().st_mtime for f in files), default=d.stat().st_mtime)
            if t > latest_t:
                latest, latest_t = d, t
    current = None
    if latest is not None:
        done = {f.stem for f in latest.glob("*.json")} if latest.exists() else set()
        final = st.get("phase") == "final" or (st.get("phase") == "done" and not (done & set(cases)))
        of = len(cfg.get("final_cases") or []) if final else len(cases)
        current = {"hash": latest.parent.name, "trials": latest.name,
                   "done": len(done) if final else (len(done & set(cases)) or len(done)), "of": of, "is_incumbent": bool(incumbent_ref and incumbent_ref["hash"] == latest.parent.name),
                   "last_write": latest_t}
        current["cases"] = _case_table(run, {"hash": latest.parent.name, "trials": latest.name[1:]})
        current["label"] = label

    history = []
    hp = run / "history.jsonl"
    if hp.exists():
        for line in hp.read_text().splitlines():
            if line.strip():
                e = json.loads(line)
                history.append({k: e.get(k) for k in ("round", "candidate_id", "component", "hypothesis", "outcome",
                                                      "reason", "delta_S", "delta_C", "improved", "regressed",
                                                      "cost_usd", "diff")})

    log_lines = []
    for ext in (".out", ".log"):
        p = RUNS / f"{name}{ext}"
        if p.exists():
            lines = p.read_text(errors="ignore").splitlines()[-400:]
            log_lines = [ln[:300] for ln in lines if not _NOISE.search(ln)][-40:]
            break

    report = (run / "report" / "report.md")
    cand = st.get("candidate") or {}
    hb_time = hb.get("time")
    hb_age = None
    if hb_time:
        try:
            hb_age = time.time() - datetime.fromisoformat(hb_time).timestamp()
        except Exception:
            pass
    return {
        "name": name,
        "phase": st.get("phase"), "round": st.get("round"), "max_rounds": cfg.get("max_rounds"),
        "spent_usd": st.get("spent_usd") or 0, "budget_usd": cfg.get("budget_usd"),
        "S_star": st.get("S_star"), "delta": st.get("delta"), "delta_cost": st.get("delta_cost"),
        "delta_esc": st.get("delta_esc"), "accepted": st.get("accepted") or [],
        "stop_reason": st.get("stop_reason"), "stalled_rounds": st.get("stalled_rounds"),
        "config": {k: cfg.get(k) for k in ("trials", "calibration_trials", "max_stall", "cost_band", "acceptance",
                                           "transfer_margin", "parallel_lanes")},
        "n_evolve": len(cfg.get("evolve_cases") or []), "n_guards": len(cfg.get("guard_cases") or []),
        "n_transfer": len(cfg.get("transfer_cases") or []), "n_final": len(cfg.get("final_cases") or []),
        "running": _alive(hb.get("pid")) if hb.get("pid") else False,
        "heartbeat_age_s": hb_age, "seconds_since_progress": hb.get("seconds_since_progress"),
        "trials_finished_this_session": hb.get("trials_finished_this_session"),
        "candidate": {k: cand.get(k) for k in ("id", "component", "hypothesis")} if cand else None,
        "incumbent_cases": _case_table(run, incumbent_ref),
        "current": current, "history": history,
        "timing": (st.get("timing") or {}).get("phases", [])[-30:],
        "health": st.get("health") or {},
        "report": report.read_text() if report.exists() else None,
        "log": log_lines,
        "now": datetime.now(timezone.utc).isoformat(),
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            return self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/runs":
            return self._send(200, json.dumps(runs()).encode(), "application/json")
        if self.path.startswith("/api/run/"):
            name = unquote(self.path[len("/api/run/"):])
            if "/" in name or name.startswith(".") or not (RUNS / name / "state.json").exists():
                return self._send(404, b"no such run", "text/plain")
            return self._send(200, json.dumps(run_detail(name), default=str).encode(), "application/json")
        self._send(404, b"not found", "text/plain")

    def log_message(self, *_args) -> None:
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Optimizer dashboard on {url} (Ctrl-C to stop)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    server.serve_forever()
