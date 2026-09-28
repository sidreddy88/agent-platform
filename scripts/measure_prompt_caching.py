"""
Step F: what prompt caching saves on DiagnosisAgent, same model, same harness.

    python scripts/measure_prompt_caching.py --out runs/harness/caching

Replays the same cases twice, once with prompt caching (production) and once
with it off (PROMPT_CACHE=0), and reports cost per trial, tokens by billing
type and cache hit rate for each, paired by case. This is the only valid
source for a "caching cut cost X%" claim: the older $1.64 -> $0.12 per trial
drop mixed caching with a Sonnet 4.6 -> 5 switch.

Real API spend: the uncached arm is the expensive one (~$1 per trial on
Sonnet 5). Defaults: 12 cases (one or two per repo), 1 trial per arm.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def pick_cases(split: dict, n: int) -> list[str]:
    """Spread across repos: round-robin over the evolve set's repos."""
    stats = split["case_stats"]
    pool = split["evolve"]["guards"] + split["evolve"]["failing"] + split["evolve"].get("hard", [])
    by_repo: dict[str, list[str]] = {}
    for c in pool:
        by_repo.setdefault(stats[c]["repo"], []).append(c)
    out: list[str] = []
    while len(out) < n and any(by_repo.values()):
        for repo in sorted(by_repo):
            if by_repo[repo] and len(out) < n:
                out.append(by_repo[repo].pop(0))
    return out


async def run_arm(cases: list[str], cached: bool, concurrency: int) -> dict:
    from app.agents.harness import DEFAULT_ROOT
    from app.harness_optimizer.evaluator import ReplayEvaluator

    os.environ["PROMPT_CACHE"] = "1" if cached else "0"
    ev = ReplayEvaluator()
    gate = asyncio.Semaphore(concurrency)

    async def one(case):
        async with gate:
            out = await ev.evaluate(DEFAULT_ROOT / "diagnosis", [case], 1)
            t = out.trajectories[0] if out.trajectories else {}
            return case, {"verdict": out.result.per_case[case].verdicts[0], "cost_usd": out.cost_usd,
                          "turns": len(t.get("steps") or []), "cost": t.get("cost", {})}

    return dict(await asyncio.gather(*(one(c) for c in cases)))


def summarize(arm: dict) -> dict:
    n = len(arm)
    tok: dict[str, int] = {}
    by_type: dict[str, float] = {}
    for r in arm.values():
        for k, v in (r["cost"].get("tokens") or {}).items():
            tok[k] = tok.get(k, 0) + v
        for k, v in (r["cost"].get("by_billing_type") or {}).items():
            by_type[k] = by_type.get(k, 0.0) + v
    reads, writes, inp = tok.get("cache_read", 0), tok.get("cache_write", 0), tok.get("input", 0)
    return {"trials": n,
            "cost_per_trial_usd": sum(r["cost_usd"] for r in arm.values()) / n,
            "pass_rate": sum(r["verdict"] == "PASS" for r in arm.values()) / n,
            "mean_turns": sum(r["turns"] for r in arm.values()) / n,
            "cache_hit_rate": reads / (reads + writes + inp) if reads + writes + inp else 0.0,
            "cost_by_billing_type_usd": {k: round(v, 4) for k, v in by_type.items()},
            "tokens": tok}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "runs" / "harness" / "caching")
    ap.add_argument("--cases", type=int, default=12)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    split = json.loads((ROOT / "app" / "evals" / "harness_split.json").read_text())
    cases = pick_cases(split, args.cases)
    args.out.mkdir(parents=True, exist_ok=True)
    result = {"cases": cases}
    for label, cached in (("cached", True), ("uncached", False)):
        path = args.out / f"{label}.json"
        arm = json.loads(path.read_text()) if path.exists() else asyncio.run(run_arm(cases, cached, args.concurrency))
        path.write_text(json.dumps(arm, indent=1))           # a crash between arms keeps the first
        result[label] = summarize(arm)
    c, u = result["cached"]["cost_per_trial_usd"], result["uncached"]["cost_per_trial_usd"]
    # Paired by case: turns differ run to run, so also compare cost per turn.
    result["saving"] = 1 - c / u if u else None
    cpt = lambda a: a["cost_per_trial_usd"] / a["mean_turns"] if a["mean_turns"] else 0.0
    result["saving_per_turn"] = 1 - cpt(result["cached"]) / cpt(result["uncached"]) if cpt(result["uncached"]) else None
    (args.out / "summary.json").write_text(json.dumps(result, indent=1))
    print(json.dumps({k: result[k] for k in ("saving", "saving_per_turn")}, indent=1))
    for label in ("cached", "uncached"):
        a = result[label]
        print(f"{label:9s} ${a['cost_per_trial_usd']:.3f}/trial  pass {a['pass_rate']:.2f}  "
              f"turns {a['mean_turns']:.1f}  cache hit {a['cache_hit_rate']:.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
