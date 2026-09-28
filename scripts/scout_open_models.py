"""
Open-model scout: does DiagnosisAgent's harness work on cheaper open-weight models?

    python scripts/scout_open_models.py --out runs/harness/scout

Replays the original 17 evolve cases (13 failing + 4 guards) once per model
with the retry policy off (the raw model, not the retry), routing DiagnosisAgent
through LLM_MODEL_DIAGNOSIS. Compares each model with Sonnet 5 on the same
cases (r5 round 0, 3 trials each). Reports pass rate, cost per diagnosis, turns,
how often a run never submitted, and how often tool calls failed (malformed
input, unknown tool). Those failure modes are what harness work could fix.

Go/no-go for the open-model track (harness-evolution-v2.md §0.9): a model
continues only if its tool use is clean and it passes >= ~40% here.
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

# Serverless on Together (probed 2026-09-26); coding/agentic-capable, 128k+ context.
MODELS = [
    "together_ai/deepseek-ai/DeepSeek-V4.1-Flash",
    "together_ai/MiniMaxAI/MiniMax-M3",
    "together_ai/zai-org/GLM-5.3-Flash",
    "together_ai/openai/gpt-oss-120b",
]
SONNET_REF = ROOT / "runs" / "harness" / "r5" / "evals" / "dae4b6ab31ea7604" / "k3"
RAW_HARNESS = ROOT / "runs" / "harness" / "_harnesses" / "retry0"


def _tool_failed(out: str) -> bool:
    o = out.lstrip()
    return o.startswith("Error") or "failed:" in o[:200] or o.startswith("Unknown tool")


async def run_model(model: str, cases: list[str], concurrency: int) -> dict:
    from app.harness_optimizer import grader
    from app.harness_optimizer.evaluator import ProviderFailure, ReplayEvaluator

    os.environ["LLM_MODEL_DIAGNOSIS"] = model
    ev = ReplayEvaluator()
    gate = asyncio.Semaphore(concurrency)

    async def one(case):
        async with gate:
            try:
                out = await ev.evaluate(RAW_HARNESS, [case], 1)
            except ProviderFailure as exc:
                return case, {"verdict": "INFRA", "error": str(exc)[:300], "cost_usd": exc.cost_usd}
            t = out.trajectories[0] if out.trajectories else {}
            steps = t.get("steps") or []
            return case, {
                "verdict": out.result.per_case[case].verdicts[0],
                "cost_usd": out.cost_usd,
                "turns": len(steps),
                "no_submission": not grader.grade(t).accepted if t else True,
                "tool_calls": len(steps),
                "tool_failures": sum(_tool_failed(str(s.get("output") or "")) for s in steps),
                "cache_hit_rate": (t.get("cost") or {}).get("cache_hit_rate"),
                "detail": t.get("detail", "")[:200],
            }

    return dict(await asyncio.gather(*(one(c) for c in cases)))


def summarize(arm: dict) -> dict:
    ok = [r for r in arm.values() if r["verdict"] != "INFRA"]
    n = len(ok) or 1
    calls = sum(r.get("tool_calls", 0) for r in ok)
    return {"cases": len(arm), "infra": len(arm) - len(ok),
            "pass_rate": sum(r["verdict"] == "PASS" for r in ok) / n,
            "cost_per_diagnosis_usd": sum(r["cost_usd"] for r in ok) / n,
            "mean_turns": sum(r.get("turns", 0) for r in ok) / n,
            "no_submission_rate": sum(r.get("no_submission", False) for r in ok) / n,
            "tool_failure_rate": sum(r.get("tool_failures", 0) for r in ok) / calls if calls else None}


def sonnet_reference(cases: list[str]) -> dict:
    per = [json.loads((SONNET_REF / f"{c}.json").read_text())["case"] for c in cases]
    trials = sum(p["trials"] for p in per)
    return {"model": "claude-sonnet-5 (r5 round 0, 3 trials)",
            "pass_rate": sum(p["passes"] / p["trials"] for p in per) / len(per),
            "cost_per_diagnosis_usd": sum(p["cost_usd"] for p in per) / trials,
            "no_submission_rate": sum(p["escalations"] for p in per) / trials}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "runs" / "harness" / "scout")
    ap.add_argument("--models", nargs="*", default=MODELS)
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    split = json.loads((ROOT / "app" / "evals" / "harness_split.json").read_text())
    cases = split["evolve"]["failing"] + split["evolve"]["guards"]
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"cases": cases, "sonnet": sonnet_reference(cases), "models": {}}
    for model in args.models:
        path = args.out / (model.replace("/", "__") + ".json")
        arm = json.loads(path.read_text()) if path.exists() else asyncio.run(run_model(model, cases, args.concurrency))
        path.write_text(json.dumps(arm, indent=1))
        report["models"][model] = summarize(arm)
        s = report["models"][model]
        tf = "-" if s["tool_failure_rate"] is None else f"{s['tool_failure_rate']:.0%}"
        print(f"{model}: pass {s['pass_rate']:.2f}  ${s['cost_per_diagnosis_usd']:.3f}/diagnosis  "
              f"turns {s['mean_turns']:.1f}  no-submission {s['no_submission_rate']:.0%}  "
              f"tool failures {tf}  infra {s['infra']}", flush=True)
    s = report["sonnet"]
    print(f"{s['model']}: pass {s['pass_rate']:.2f}  ${s['cost_per_diagnosis_usd']:.3f}/diagnosis  "
          f"no-submission {s['no_submission_rate']:.0%}")
    (args.out / "summary.json").write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
