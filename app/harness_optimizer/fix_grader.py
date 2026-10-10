"""
Trajectory grader for FixGenerationAgent runs: what went wrong or was wasted in
one fix loop, from its step log alone (FixGenerationAgent._fix_steps). No LLM,
no API cost. The fix-agent counterpart of grader.py, same interface (grade,
summarize, render) so evidence.py and loop.py can use either.

- edits recorded vs rejected at the call (old text not in the file), and
  rejection streaks with the same message (the model not correcting itself)
- redundant identical calls (same tool, same input)
- tool errors and empty results (failed reads, searches with no hits)
- ending: whether the run produced a patch ("accepted"), declined with no_edit,
  or ran out without an edit, the fix-step escalation failure mode
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field

EDIT_TOOLS = ("apply_edit", "patch_line")
REJECTED = ("ERROR: apply_edit was NOT recorded", "ERROR: patch_line was NOT recorded")
ERRORS = ("Error reading", "Search failed", "Call graph lookup failed", "Unknown tool", "[sandbox error")
EMPTY = ("No results", "No callers found")


@dataclass
class Grade:
    instance_id: str
    trial: int | None
    verdict: str
    turns: int
    cost_usd: float
    tool_calls: dict = field(default_factory=dict)
    edits_recorded: int = 0
    edits_rejected: int = 0
    repeated_rejections: int = 0
    redundant_calls: int = 0
    tool_errors: int = 0
    empty_results: int = 0
    declined: bool = False
    accepted: bool = False          # produced a patch
    flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def grade(rec: dict) -> Grade:
    steps = [s for s in rec.get("steps") or [] if s.get("name")]
    outs = [(s["name"], str(s.get("output") or "").lstrip()) for s in steps]
    seen: Counter[tuple] = Counter((s["name"], repr(sorted((s.get("input") or {}).items()))) for s in steps)
    redundant = sum(n - 1 for (name, _), n in seen.items() if n > 1 and name not in EDIT_TOOLS)

    rejected = [out for name, out in outs if name in EDIT_TOOLS and out.startswith(REJECTED)]
    repeated = sum(1 for a, b in zip(rejected, rejected[1:]) if a[:200] == b[:200])
    recorded = sum(1 for name, out in outs if name in EDIT_TOOLS and out.startswith("✓"))
    declined = any(name == "no_edit" for name, _ in outs)
    accepted = (rec.get("detail") or "") != "no patch"

    g = Grade(
        instance_id=rec.get("instance_id", "?"), trial=rec.get("trial"), verdict=rec.get("verdict", "?"),
        turns=rec.get("turns") or len(rec.get("llm_calls") or []) or len(steps),
        cost_usd=float((rec.get("cost") or {}).get("cost_usd") or 0.0),
        tool_calls=dict(Counter(n for n, _ in outs)),
        edits_recorded=recorded, edits_rejected=len(rejected), repeated_rejections=repeated,
        redundant_calls=redundant,
        tool_errors=sum(1 for _, out in outs if out.startswith(ERRORS)),
        empty_results=sum(1 for _, out in outs if out.startswith(EMPTY)),
        declined=declined, accepted=accepted,
    )
    if g.edits_rejected:
        g.flags.append(f"{g.edits_rejected} edit(s) rejected at the call (old text not in the file)"
                       + (f", {g.repeated_rejections} repeating the same rejection" if repeated else ""))
    if g.redundant_calls:
        g.flags.append(f"{g.redundant_calls} redundant identical call(s)")
    if g.tool_errors:
        g.flags.append(f"{g.tool_errors} tool error(s)")
    if not g.accepted:
        g.flags.append("declined with no_edit" if declined else
                       f"ended without a patch ({g.turns} turns, {g.edits_rejected} rejected edit(s))")
    return g


def summarize(grades: list[Grade]) -> dict:
    if not grades:
        return {"trajectories": 0}
    by_verdict: dict[str, list[Grade]] = {}
    for g in grades:
        by_verdict.setdefault(g.verdict, []).append(g)
    return {
        "trajectories": len(grades),
        "by_verdict": {v: {"n": len(gs), "mean_turns": round(statistics.fmean(g.turns for g in gs), 1),
                           "mean_cost_usd": round(statistics.fmean(g.cost_usd for g in gs), 3)}
                       for v, gs in sorted(by_verdict.items())},
        "no_patch": sum(not g.accepted for g in grades),
        "declined": sum(g.declined for g in grades),
        "edits_rejected": sum(g.edits_rejected for g in grades),
        "repeated_rejections": sum(g.repeated_rejections for g in grades),
        "redundant_calls": sum(g.redundant_calls for g in grades),
        "tool_errors": sum(g.tool_errors for g in grades),
        "empty_results": sum(g.empty_results for g in grades),
        "tool_calls": dict(sum((Counter(g.tool_calls) for g in grades), Counter()).most_common()),
    }


def render(summary: dict) -> str:
    if not summary.get("trajectories"):
        return "No trajectories graded."
    s = summary
    lines = [f"Graded {s['trajectories']} fix runs."]
    for v, d in s["by_verdict"].items():
        lines.append(f"  {v}: {d['n']} runs, mean {d['mean_turns']} turns, mean ${d['mean_cost_usd']:.3f}")
    lines += [
        f"Ended without a patch: {s['no_patch']}/{s['trajectories']} ({s['declined']} declined with no_edit)",
        f"Edits rejected at the call: {s['edits_rejected']} ({s['repeated_rejections']} repeating the same rejection)",
        f"Redundant identical calls: {s['redundant_calls']}; tool errors: {s['tool_errors']}; "
        f"empty results: {s['empty_results']}",
        "Tool calls: " + ", ".join(f"{k}={v}" for k, v in s["tool_calls"].items()),
    ]
    return "\n".join(lines)
