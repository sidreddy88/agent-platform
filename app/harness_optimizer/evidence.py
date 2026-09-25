"""
Evidence for the proposer: what the agent actually did under the incumbent.

Built from the per-request trajectory records the evaluator captures
(scripts/eval_swebench_diagnosis.py, trajectory_sink). Three layers, from
aggregate to specific:

1. Per case: verdict per trial, turns used, cost.
2. Waste signals: tool-call counts, identical repeated calls, grounding-gate
   rejections, and cost by prompt source (scripts/analyze_cost_by_source.py),
   so the proposer can see where turns and money go.
3. A few failing trajectories, turn by turn (tool, input, observation size,
   rejections), so an edit can be aimed at a concrete failure, as in GEPA.

Case ids, repo names and paths appear here because the proposer needs to
understand failures; the critic's precheck is what keeps them out of the
harness itself.
"""
from __future__ import annotations

from collections import Counter

from app.harness_optimizer.acceptance import EvalResult


def build(result: EvalResult, trajectories: list[dict], max_failing: int = 3,
          max_chars: int = 20_000) -> str:
    from scripts.analyze_cost_by_source import analyze

    lines = [f"Incumbent: S={result.S:.3f} (mean per-case pass rate), cost/trial=${result.C:.3f}, "
             f"escalation rate={result.escalation_rate:.1%}", "", "Per case:"]
    turns: dict[str, list[int]] = {}
    for rec in trajectories:
        turns.setdefault(rec["instance_id"], []).append(len(rec.get("llm_calls") or []))
    for cid, cr in sorted(result.per_case.items()):
        lines.append(f"  {cid}: {cr.verdicts} turns={turns.get(cid, [])} cost=${cr.cost_usd:.2f}")

    calls = Counter()
    repeats = Counter()
    rejections = 0
    for rec in trajectories:
        seen = set()
        for step in rec.get("steps") or []:
            calls[step["name"]] += 1
            key = (step["name"], step.get("input"))
            if key in seen:
                repeats[step["name"]] += 1
            seen.add(key)
            if str(step.get("output", "")).lstrip().startswith("REJECTED"):
                rejections += 1
    lines += ["", "Tool calls across all trials: " + ", ".join(f"{k}={v}" for k, v in calls.most_common()),
              "Identical repeated calls (same tool, same input, same trial): "
              + (", ".join(f"{k}={v}" for k, v in repeats.most_common()) or "none"),
              f"Grounding-gate rejections: {rejections}"]

    if trajectories:
        rep = analyze(trajectories)
        lines += ["", "Cost by prompt source (share of spend):"]
        lines += [f"  {r['source']}: {r['share']:.1%} (${r.get('cost', 0):.2f})" for r in rep["by_source"][:10]]

    failing = [r for r in trajectories if r.get("verdict") != "PASS"][:max_failing]
    for rec in failing:
        lines += ["", f"=== Failing trajectory: {rec['instance_id']} trial {rec.get('trial')} "
                      f"({rec.get('verdict')}: {rec.get('detail', '')[:120]}) ==="]
        for step in rec.get("steps") or []:
            out = str(step.get("output", ""))
            note = f" -> {out[:300]!r}" if out.lstrip().startswith("REJECTED") else f" -> {len(out)} chars"
            lines.append(f"  turn {step.get('iteration')}: {step['name']}({str(step.get('input'))[:120]}){note}")

    text = "\n".join(lines)
    return text if len(text) <= max_chars else text[:max_chars] + "\n[evidence truncated]"
