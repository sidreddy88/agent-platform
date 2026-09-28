"""
Diagnosis regression gate v2 (see app/evals/gate_v2.py for the design).

The replays themselves run through the optimizer's round-0 runner
(scripts/optimize_harness.py --rounds 0), which already has per-repo lanes,
a per-case result cache, a hard budget cap, tripwires and provider-failure
stops. This script does everything around it:

    python scripts/gate_v2.py pool                         # (re)build app/evals/gate_v2_cases.json
    python scripts/gate_v2.py shard-file --index 3 --of 20 --out /tmp/shard.json
    python scripts/optimize_harness.py --run-dir runs/gate/s3 --cases-file /tmp/shard.json \\
        --rounds 0 --calibration-trials 2 --budget 5 --parallel 2 --lane-width 3
    python scripts/gate_v2.py collect --run-dir runs/gate/s3 --trials 2 --out results/s3.json
    python scripts/gate_v2.py aggregate --results results/*.json      # exit 1 if the gate fails
    python scripts/gate_v2.py baseline --results cal/*.json --main-sha <sha>   # write the baseline
    python scripts/gate_v2.py power --pr-trials 2                     # size the gate from the baseline
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evals import gate_v2  # noqa: E402

HARNESS = ROOT / "app" / "agents" / "harness" / "diagnosis"


def _read_results(paths: list[str]) -> gate_v2.Verdicts:
    parts = []
    for p in paths:
        data = json.loads(Path(p).read_text())
        parts.append(data["verdicts"])
    return gate_v2.merge(*parts)


def cmd_pool(args) -> int:
    pool = gate_v2.build_pool()
    gate_v2.POOL.write_text(json.dumps(pool, indent=1) + "\n")
    print(f"{len(pool['cases'])} cases, excluding {pool['excluded_repos']}")
    return 0


def cmd_shard_file(args) -> int:
    pool = gate_v2.load_pool()
    cases = gate_v2.shard(pool["cases"], args.of, args.index, pool["repos"], gate_v2.REPO_WEIGHTS)
    Path(args.out).write_text(json.dumps({"evolve": cases, "guards": []}))
    print(f"shard {args.index}/{args.of}: {len(cases)} cases")
    return 0


def cmd_collect(args) -> int:
    from app.harness_optimizer.candidates import content_hash

    h = content_hash(Path(args.harness))
    verdicts = gate_v2.collect([Path(d) for d in args.run_dir], h, args.trials)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps({"harness_hash": h, "trials": args.trials,
                                          "verdicts": verdicts}, indent=1))
    print(f"{len(verdicts)} cases collected for harness {h}")
    return 0


def cmd_timing(args) -> int:
    """One shard's wall clock, replay count and cost, for sizing the full run."""
    cases = trials = 0
    cost = 0.0
    for f in (Path(args.run_dir) / "evals").rglob("*.json"):
        c = json.loads(f.read_text())["case"]
        cases += 1
        trials += c["trials"]
        cost += c.get("cost_usd") or 0.0
    out = {"shard": args.shard, "seconds": args.seconds, "cases": cases, "replays": trials, "cost_usd": cost}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(out))
    print(out)
    return 0


def cmd_timing_report(args) -> int:
    rows = [json.loads(Path(p).read_text()) for p in args.results]
    if not rows:
        print("no timing results")
        return 1
    lines = ["## Gate v2 probe", "", "| Shard | Cases | Replays | Minutes | Cost |", "|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: r["shard"]):
        lines.append(f"| {r['shard']} | {r['cases']} | {r['replays']} | {r['seconds'] / 60:.1f} | ${r['cost_usd']:.2f} |")
    replays = sum(r["replays"] for r in rows)
    cost = sum(r["cost_usd"] for r in rows)
    worst = max(r["seconds"] for r in rows) / 60
    full = len(gate_v2.load_pool()["cases"]) * (replays / max(1, sum(r["cases"] for r in rows)))
    lines += ["", f"Cost per replay ${cost / replays:.4f}; a full run ({full:.0f} replays) ≈ "
                  f"${cost / replays * full:.0f}. Slowest shard {worst:.1f} min (20 shards run in parallel)."]
    text = "\n".join(lines)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")
    return 0


def cmd_aggregate(args) -> int:
    pool = gate_v2.load_pool()
    base = gate_v2.load_baseline(Path(args.baseline))
    res = gate_v2.paired_test(_read_results(args.results), base["cases"], pool["cases"], pool["repos"],
                              z_alpha=base.get("z_alpha", gate_v2.Z_ALPHA))
    text = gate_v2.report(res)
    text += (f"\n\nBaseline: main {base.get('main_sha', '?')[:7]}, {base.get('model')}, "
             f"harness {base.get('harness_hash')}, {base.get('trials')} trials/case, "
             f"measured {base.get('measured_at')}.")
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")
    return 0 if res.passed else 1


def cmd_baseline(args) -> int:
    from datetime import datetime, timezone

    from app.harness_optimizer.candidates import content_hash
    from app.services.llm_gateway import llm_gateway

    verdicts = _read_results(args.results)
    pool = gate_v2.load_pool()["cases"]
    missing = [c for c in pool if not verdicts.get(c)]
    if missing:
        print(f"{len(missing)} pool cases have no trials (e.g. {missing[:5]}): not writing a baseline")
        return 1
    trials = sorted({len(verdicts[c]) for c in pool})
    out = {
        "_doc": "Main's per-case verdicts for gate v2. Refresh after any merge that changes "
                "diagnosis (harness, diagnosis.py, base.py, or the diagnosis model).",
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "main_sha": args.main_sha,
        "model": llm_gateway._get_routing("diagnosis")[1],
        "harness_hash": content_hash(HARNESS),
        "trials": trials[0] if len(trials) == 1 else trials,
        "cases": {c: verdicts[c] for c in pool},
    }
    out["pr_trials"] = args.pr_trials
    out["z_alpha"] = round(gate_v2.calibrate_threshold(out["cases"], pool, args.pr_trials), 3)
    Path(args.out).write_text(json.dumps(out, indent=1) + "\n")
    passes = sum(v.count("PASS") for v in out["cases"].values())
    total = sum(len(v) for v in out["cases"].values())
    print(f"baseline: {len(pool)} cases, {total} trials, pass rate {passes / total:.1%}, "
          f"calibrated threshold z < -{out['z_alpha']} for {args.pr_trials} PR trials -> {args.out}")
    return 0


def cmd_power(args) -> int:
    pool = gate_v2.load_pool()["cases"]
    bl = gate_v2.load_baseline(Path(args.baseline))
    base = bl["cases"]
    z = bl.get("z_alpha") if bl.get("pr_trials") == args.pr_trials else None
    if z is None:
        z = gate_v2.calibrate_threshold(base, pool, args.pr_trials, sims=args.sims)
    print(f"threshold: fail if z < -{z:.3f} (calibrated for a 5% false-alarm rate)")
    m = min(len(base[c]) for c in pool)
    if m > args.pr_trials:
        fa = gate_v2.false_alarm_rate(base, pool, args.pr_trials, sims=args.sims, z_alpha=z)
        print(f"false-alarm rate, measured by splitting baseline trials: {fa:.1%}")
    else:
        print(f"(false alarms need more than {args.pr_trials} baseline trials per case; have {m})")
    print(f"\nregression -> detection with {args.pr_trials} PR trials per case (modelled):")
    for shift in (0.0, 0.3, 0.5, 0.8, 1.2):
        det, drop = gate_v2.power(base, pool, args.pr_trials, shift, sims=args.sims, seed=7, z_alpha=z)
        print(f"  {drop * 100:4.1f}pp drop  ->  caught {det:.0%}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pool")
    s = sub.add_parser("shard-file")
    s.add_argument("--index", type=int, required=True)
    s.add_argument("--of", type=int, required=True)
    s.add_argument("--out", required=True)
    s = sub.add_parser("collect")
    s.add_argument("--run-dir", action="append", required=True)
    s.add_argument("--trials", type=int, required=True)
    s.add_argument("--harness", default=str(HARNESS))
    s.add_argument("--out", required=True)
    s = sub.add_parser("timing")
    s.add_argument("--run-dir", required=True)
    s.add_argument("--shard", type=int, required=True)
    s.add_argument("--seconds", type=int, required=True)
    s.add_argument("--out", required=True)
    s = sub.add_parser("timing-report")
    s.add_argument("--results", nargs="+", required=True)
    s = sub.add_parser("aggregate")
    s.add_argument("--results", nargs="+", required=True)
    s.add_argument("--baseline", default=str(gate_v2.BASELINE))
    s = sub.add_parser("baseline")
    s.add_argument("--results", nargs="+", required=True)
    s.add_argument("--main-sha", required=True)
    s.add_argument("--pr-trials", type=int, default=2, help="trials per case the gate's PR runs will use")
    s.add_argument("--out", default=str(gate_v2.BASELINE))
    s = sub.add_parser("power")
    s.add_argument("--baseline", default=str(gate_v2.BASELINE))
    s.add_argument("--pr-trials", type=int, default=2)
    s.add_argument("--sims", type=int, default=1000)
    args = ap.parse_args()
    return {"pool": cmd_pool, "shard-file": cmd_shard_file, "collect": cmd_collect,
            "aggregate": cmd_aggregate, "timing": cmd_timing, "timing-report": cmd_timing_report, "baseline": cmd_baseline, "power": cmd_power}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
