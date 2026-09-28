"""
Summarize the r9 campaign (DeepSeek-V4.1-Flash optimizer run) once it finishes.

    python scripts/summarize_r9.py            # prints the summary, writes runs/harness/r9-deepseek/summary.json

Reads only files on disk, no API calls:
- runs/harness/r9-deepseek/{state.json, history.jsonl, report/report.json}: rounds, timing, and
  the held-out report (original vs final harness, 66 cases x 3 trials)
- runs/harness/retry-confirm/: Sonnet 5 on the same 66 held-out cases (first attempt and with
  retry), for the head-to-head
Prints: sessions/hours, the rounds table, the held-out verdict with CIs, the rerun-baseline
comparison, cost per diagnosis, and Sonnet vs DeepSeek on held-out.
"""
from __future__ import annotations

import glob
import json
import random
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
R = ROOT / "runs" / "harness" / "r9-deepseek"


def _boot(diffs, n=4000, seed=0):
    rng = random.Random(seed)
    b = sorted(statistics.fmean(rng.choice(diffs) for _ in diffs) for _ in range(n))
    return b[int(0.025 * n)], b[int(0.975 * n) - 1]


def main() -> int:
    from app.harness_optimizer.candidates import content_hash

    s = json.loads((R / "state.json").read_text())
    out = {"phase": s["phase"], "stop_reason": s.get("stop_reason"), "spent_usd": round(s["spent_usd"], 2),
           "accepted": s["accepted"], "sessions": [
               {k: x.get(k) for k in ("start", "end", "seconds", "replays", "ended_because")}
               for x in s["timing"]["sessions"]]}
    out["rounds"] = [{k: h.get(k) for k in ("round", "component", "outcome", "delta_S", "delta_C", "cost_usd")}
                     for h in map(json.loads, (R / "history.jsonl").read_text().splitlines())]
    rep_path = R / "report" / "report.json"
    out["report"] = json.loads(rep_path.read_text()) if rep_path.exists() else None

    # Held-out per case, both arms (k=3), plus Sonnet on the same cases (retry-confirm, k=2).
    cases = s["config"]["final_cases"]
    arms = {}
    for name, d in (("original", R / "original"), ("evolved", R / "incumbent")):
        h = content_hash(d)
        arms[name] = {c: json.loads(p.read_text())["case"] for c in cases
                      if (p := R / "evals" / h / "k3" / f"{c}.json").exists()}
    son = {}
    for f in glob.glob(str(ROOT / "runs/harness/retry-confirm/evals/*/k2/*.json")):
        ts = json.loads(Path(f).read_text())["trajectories"]
        son[Path(f).stem] = [(t["verdict"] == "PASS" and len(t.get("attempts") or []) <= 1,
                              t["verdict"] == "PASS", t["cost"]["cost_usd"]) for t in ts]
    both = [c for c in cases if c in arms["original"] and c in arms["evolved"]]
    rate = lambda v: v["passes"] / v["trials"]
    if both:
        d = [rate(arms["evolved"][c]) - rate(arms["original"][c]) for c in both]
        lo, hi = _boot(d)
        cost = lambda a: sum(a[c]["cost_usd"] for c in both) / sum(a[c]["trials"] for c in both)
        out["heldout"] = {
            "cases": len(both),
            "original_pass": statistics.fmean(rate(arms["original"][c]) for c in both),
            "evolved_pass": statistics.fmean(rate(arms["evolved"][c]) for c in both),
            "diff": statistics.fmean(d), "ci95": [lo, hi],
            "original_cost_per_trial": cost(arms["original"]), "evolved_cost_per_trial": cost(arms["evolved"]),
        }
        sc = [c for c in both if c in son]
        if sc:
            sf = [statistics.fmean(x[1] for x in son[c]) for c in sc]
            d2 = [rate(arms["evolved"][c]) - v for c, v in zip(sc, sf)]
            lo2, hi2 = _boot(d2)
            out["vs_sonnet_with_retry"] = {"cases": len(sc), "sonnet": statistics.fmean(sf),
                                           "deepseek_evolved": statistics.fmean(rate(arms["evolved"][c]) for c in sc),
                                           "diff": statistics.fmean(d2), "ci95": [lo2, hi2],
                                           "sonnet_cost_per_trial": statistics.fmean(x[2] for c in sc for x in son[c])}
    (R / "summary.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
