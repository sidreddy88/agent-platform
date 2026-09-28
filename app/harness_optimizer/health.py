"""
Tripwires: stop a long run the moment its measurements stop being trustworthy.

The worst loss in this project so far wasn't a crash. It was a broken tool
that looked like data: verify_symbol_in_repo returned NOT_FOUND on 87 of 87
calls (an invalid GitHub token, swallowed as "no results"), and two whole
runs were scored on it before anyone read the traces. An 8-hour unattended
run can spend its budget the same way. So while cases finish, the loop checks
the recent trajectories against what a healthy run looks like and pauses
(resumably, like a provider failure) when something is off:

- a tool's failure rate (errors, NOT_FOUND, empty results) far above normal
- replays erroring or timing out instead of producing a verdict
- every guard case failing (the always-pass cases: the easy path is broken)
- cost per trial far above normal, or zero (a price missing from the meter)
- the pass rate collapsing, in round 0 only (a candidate is allowed to be
  bad; that's what the acceptance rules are for)

"Normal" comes from BASELINE, measured on the pilot run's round 0 (r4, 102
replays of the original harness on Sonnet 5). Thresholds are deliberately
loose: a tripwire that fires on noise would end unattended runs for nothing.
They're sized to catch breakage, which in every case seen so far was
all-or-nothing (100% NOT_FOUND, every call to the wrong graph), not drift.
"""
from __future__ import annotations

from dataclasses import dataclass

# Measured on runs/harness/r4-20260925 round 0 (and its first two candidates):
# failure rate per tool, errors / NOT_FOUND / empty results over all calls.
BASELINE = {
    "tool_failure_rate": {
        "verify_symbol_in_repo": 0.0,     # 0 / 120
        "find_callers": 0.04,             # 7 / 183 "No callers found"
        "grep_codebase": 0.12,            # 70 / 603 "No matches"
        "get_file_contents": 0.03,        # 9 / 348 "Error ..."
    },
    "cost_per_trial_usd": 0.148,
    "pass_rate": 0.657,
}

MIN_CALLS = 15          # per tool, before its failure rate is judged
MIN_REPLAYS = 10


class Tripwire(RuntimeError):
    """Measurements look broken; the run pauses so a human can look."""


def _tool_failed(name: str, output: str) -> bool:
    out = output.lstrip()
    if out.startswith("Error"):
        return True
    if name == "verify_symbol_in_repo":
        return out.startswith(("NOT_FOUND", "VERIFY_ERROR"))
    if name == "find_callers":
        return out.startswith("No callers found")
    if name == "grep_codebase":
        return out.startswith("No matches")
    return False


@dataclass
class Window:
    """The most recent finished replays of one evaluation."""
    trajectories: list[dict]
    verdicts: list[str]
    costs: list[float]              # per trial
    guard_verdicts: list[str]


def check(w: Window, round0: bool, baseline: dict = BASELINE) -> list[str]:
    problems: list[str] = []
    calls: dict[str, list[bool]] = {}
    for t in w.trajectories:
        for st in t.get("steps") or []:
            calls.setdefault(st.get("name", "?"), []).append(
                _tool_failed(st.get("name", ""), str(st.get("output") or "")))
    for tool, normal in baseline["tool_failure_rate"].items():
        fails = calls.get(tool, [])
        if len(fails) >= MIN_CALLS:
            rate = sum(fails) / len(fails)
            limit = max(0.5, normal + 0.35)
            if rate >= limit:
                problems.append(f"{tool} failing on {sum(fails)}/{len(fails)} recent calls "
                                f"({rate:.0%}; normal {normal:.0%}, limit {limit:.0%})")
    n = len(w.verdicts)
    if n >= MIN_REPLAYS:
        broken = sum(v in ("ERROR", "TIMEOUT") for v in w.verdicts)
        if broken / n > 0.10:
            problems.append(f"{broken}/{n} recent replays ended in ERROR/TIMEOUT, not a verdict")
        mean_cost = sum(w.costs) / len(w.costs) if w.costs else 0.0
        if mean_cost == 0.0:
            problems.append(f"{n} recent replays cost $0: a model price is missing or the meter is off")
        elif mean_cost > 3 * baseline["cost_per_trial_usd"]:
            problems.append(f"mean cost ${mean_cost:.3f}/trial, over 3x normal "
                            f"(${baseline['cost_per_trial_usd']:.3f})")
        if round0 and n >= 20:
            rate = sum(v == "PASS" for v in w.verdicts) / n
            if rate < 0.5 * baseline["pass_rate"]:
                problems.append(f"round 0 pass rate {rate:.0%} on {n} recent replays, under half "
                                f"of the pilot's {baseline['pass_rate']:.0%}: the original harness "
                                f"shouldn't change, so something else did")
    if len(w.guard_verdicts) >= 4 and not any(v == "PASS" for v in w.guard_verdicts):
        problems.append(f"all {len(w.guard_verdicts)} guard-case trials failed")
    return problems


def smoke_problems(trajectories: list[dict], verdicts: list[str], costs: list[float]) -> list[str]:
    """Stricter checks on the handful of smoke replays that gate a long run:
    every replay must have produced tool calls, a verdict and a cost."""
    problems = []
    for t in trajectories:
        if not t.get("steps"):
            problems.append(f"{t.get('instance_id')} trial {t.get('trial')}: no tool calls recorded")
    for v in verdicts:
        if v not in ("PASS", "FAIL"):
            problems.append(f"a smoke replay ended {v}")
    if any(c <= 0 for c in costs):
        problems.append("a smoke replay cost $0: price missing or meter off")
    calls = {}
    for t in trajectories:
        for st in t.get("steps") or []:
            calls.setdefault(st.get("name", "?"), []).append(
                _tool_failed(st.get("name", ""), str(st.get("output") or "")))
    v = calls.get("verify_symbol_in_repo", [])
    if len(v) >= 3 and all(v):
        problems.append(f"verify_symbol_in_repo failed on all {len(v)} smoke calls")
    return problems
