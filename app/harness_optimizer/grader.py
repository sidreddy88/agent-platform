"""
Trajectory grader, code-checks layer: what went wrong or was wasted in one
DiagnosisAgent run, from its trajectory record alone. No LLM, no API cost.

The proposer's edits are only as good as the evidence it reflects on
(GEPA's point is that textual feedback beats a scalar score). A pass/fail
verdict says *that* a case failed; these checks say *how* the run spent its
turns, which is what a harness edit can change:

- redundant calls: the same tool with the same input twice in one run. The
  repeat's output is resent on every later turn. (Reuses
  scripts/analyze_trajectories.py.)
- unavailable tools: calls that could not work in this context, e.g.
  search_codebase with RAG not configured, grep with no local repo.
- empty results and tool errors: searches that found nothing, fetches and
  malformed inputs that failed.
- rejection loops: grounding-gate rejections where the gate's message and the
  agent's next action were both identical to the round before, so nothing
  moved. (Reuses scripts/analyze_trajectories.py.)
- ending: whether the run reached an accepted submit_diagnosis, and after how
  many submit attempts; "never accepted" is the escalation failure mode
  behind almost every gate failure.
- grounding: every file the accepted diagnosis cites should have been read
  (get_file_contents) or at least shown in a tool result (grep, symbol
  verification, search) during the run, the same notion of "retrieved" that
  DiagnosisAgent's own output validator uses.

An LLM rubric (did it form a hypothesis, did it respond to rejections
sensibly) is a later layer; these are the checks that can be computed
exactly.
"""
from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field

UNAVAILABLE = (
    ("RAG not configured", "search_codebase unavailable (RAG not configured)"),
    ("Local repo not available", "local repo not available"),
)
EMPTY = ("No matches for", "No similar past incidents found", "No relevant code found", "NOT_FOUND")
ERRORS = ("Error running tool", "Error: Action Input", "Could not fetch", "grep_codebase error",
          "VERIFY_ERROR", "RAG search error", "Error: unknown tool")
# File paths a tool result showed the agent. Mirrors what DiagnosisAgent itself
# counts as retrieved (_retrieved_file_paths): grep hits "path:line:", symbol
# verification hits "  - path  :: ...", and search/caller results "--- path:".
_SEEN_PATH = (
    re.compile(r"^([\w./-]+\.\w+):\d+:", re.M),
    re.compile(r"^\s*-\s+([\w./-]+\.\w+)\s+::", re.M),
    re.compile(r"^---\s+([\w./-]+\.\w+):\d+", re.M),
)


@dataclass
class Grade:
    instance_id: str
    trial: int | None
    verdict: str
    turns: int
    cost_usd: float
    tool_calls: dict[str, int]
    redundant_calls: int
    redundant_output_chars: int
    unavailable_calls: dict[str, int]
    empty_results: int
    tool_errors: int
    rejections: int
    unproductive_rejection_rounds: int
    submit_attempts: int
    accepted: bool
    cited_files: list[str]
    ungrounded_citations: list[str]
    flags: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


def _json(text) -> dict:
    if isinstance(text, dict):
        return text
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def _cited(submission: dict) -> list[str]:
    files = [submission.get("affected_file"), submission.get("additional_fix_file")]
    for key in ("additional_fix_targets", "blast_radius"):
        files += [t.get("file") for t in submission.get(key) or [] if isinstance(t, dict)]
    return sorted({f.lstrip("/") for f in files if isinstance(f, str) and f.strip()})


def grade(rec: dict) -> Grade:
    from scripts.analyze_trajectories import _rejections, _repeats

    steps = [s for s in rec.get("steps") or [] if s.get("name")]
    for s in steps:
        s.setdefault("output", "")
    outs = [(s["name"], str(s.get("output") or "")) for s in steps]

    reps = _repeats(steps)
    rej = _rejections(steps)
    unavailable: Counter[str] = Counter()
    for name, out in outs:
        for marker, label in UNAVAILABLE:
            if out.lstrip().startswith(marker):
                unavailable[f"{name}: {label}"] += 1
    empty = sum(1 for _, out in outs if out.lstrip().startswith(EMPTY))
    errors = sum(1 for _, out in outs if out.lstrip().startswith(ERRORS))

    submits = [s for s in steps if s["name"] == "submit_diagnosis"]
    accepted_sub = next((s for s in submits if "Diagnosis accepted" in str(s.get("output"))), None)

    read, seen = set(), set()
    for s in steps:
        out = str(s.get("output") or "")
        if s["name"] == "get_file_contents" and not out.lstrip().startswith("Could not fetch"):
            path = _json(s.get("input")).get("file_path")
            if path:
                read.add(path.lstrip("/"))
        for pat in _SEEN_PATH:
            seen |= {p.lstrip("/") for p in pat.findall(out)}
    cited = _cited(_json(accepted_sub.get("input"))) if accepted_sub else []
    ungrounded = [f for f in cited if f not in read and f not in seen]

    g = Grade(
        instance_id=rec.get("instance_id", "?"), trial=rec.get("trial"),
        verdict=rec.get("verdict", "?"), turns=len(rec.get("llm_calls") or []) or len(steps),
        cost_usd=float((rec.get("cost") or {}).get("cost_usd") or 0.0),
        tool_calls=dict(Counter(n for n, _ in outs)),
        redundant_calls=reps["redundant_calls"], redundant_output_chars=reps["wasted_output_chars"],
        unavailable_calls=dict(unavailable), empty_results=empty, tool_errors=errors,
        rejections=rej["rejections"], unproductive_rejection_rounds=rej["unproductive_rounds"],
        submit_attempts=len(submits), accepted=accepted_sub is not None,
        cited_files=cited, ungrounded_citations=ungrounded,
    )
    if g.redundant_calls:
        g.flags.append(f"{g.redundant_calls} redundant identical call(s), "
                       f"{g.redundant_output_chars} chars of repeated output resent")
    for what, n in sorted(g.unavailable_calls.items()):
        g.flags.append(f"{n} call(s) to a tool that could not work here: {what}")
    if g.tool_errors:
        g.flags.append(f"{g.tool_errors} tool error(s)")
    if g.unproductive_rejection_rounds:
        g.flags.append(f"{g.unproductive_rejection_rounds} unproductive rejection round(s): same "
                       f"gate message and same next action as the round before")
    if not g.accepted:
        g.flags.append(f"never reached an accepted submit_diagnosis ({g.submit_attempts} "
                       f"attempt(s), {g.rejections} rejection(s), {g.turns} turns)")
    if g.ungrounded_citations:
        g.flags.append(f"accepted diagnosis cites file(s) never read or seen in grep: "
                       f"{g.ungrounded_citations}")
    return g


def summarize(grades: list[Grade]) -> dict:
    """Aggregate over trajectories, split by verdict where it matters."""
    if not grades:
        return {"trajectories": 0}
    by_verdict: dict[str, list[Grade]] = {}
    for g in grades:
        by_verdict.setdefault(g.verdict, []).append(g)
    unavailable: Counter[str] = Counter()
    for g in grades:
        unavailable.update(g.unavailable_calls)
    n = len(grades)
    return {
        "trajectories": n,
        "by_verdict": {v: {"n": len(gs), "mean_turns": round(statistics.fmean(g.turns for g in gs), 1),
                           "mean_cost_usd": round(statistics.fmean(g.cost_usd for g in gs), 3)}
                       for v, gs in sorted(by_verdict.items())},
        "never_accepted": sum(not g.accepted for g in grades),
        "with_redundant_calls": sum(g.redundant_calls > 0 for g in grades),
        "redundant_calls": sum(g.redundant_calls for g in grades),
        "redundant_output_chars": sum(g.redundant_output_chars for g in grades),
        "unavailable_calls": dict(unavailable.most_common()),
        "empty_results": sum(g.empty_results for g in grades),
        "tool_errors": sum(g.tool_errors for g in grades),
        "rejections": sum(g.rejections for g in grades),
        "unproductive_rejection_rounds": sum(g.unproductive_rejection_rounds for g in grades),
        "ungrounded_accepted": sum(bool(g.ungrounded_citations) for g in grades),
        "tool_calls": dict(sum((Counter(g.tool_calls) for g in grades), Counter()).most_common()),
    }


def render(summary: dict) -> str:
    if not summary.get("trajectories"):
        return "No trajectories graded."
    s = summary
    lines = [f"Graded {s['trajectories']} trajectories."]
    for v, d in s["by_verdict"].items():
        lines.append(f"  {v}: {d['n']} runs, mean {d['mean_turns']} turns, mean ${d['mean_cost_usd']:.3f}")
    lines += [
        f"Never reached an accepted submit_diagnosis: {s['never_accepted']}/{s['trajectories']}",
        f"Runs with redundant identical calls: {s['with_redundant_calls']} "
        f"({s['redundant_calls']} calls, {s['redundant_output_chars']} chars resent)",
        "Calls to tools that could not work here: "
        + (", ".join(f"{k} x{v}" for k, v in s["unavailable_calls"].items()) or "none"),
        f"Empty results: {s['empty_results']}; tool errors: {s['tool_errors']}",
        f"Grounding-gate rejections: {s['rejections']} "
        f"({s['unproductive_rejection_rounds']} unproductive rounds)",
        f"Accepted diagnoses citing an unread file: {s['ungrounded_accepted']}",
        "Tool calls: " + ", ".join(f"{k}={v}" for k, v in s["tool_calls"].items()),
    ]
    return "\n".join(lines)
