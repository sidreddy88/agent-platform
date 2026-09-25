"""
Offline trajectory analysis for DiagnosisAgent: where do runs waste turns?

Three questions, all answered from captured ReAct step sequences, none of them
costing an API call:

  1. REPEATED CALLS   — how often does a run issue the *same* tool with the
                        *same* arguments twice? Each repeat re-enters a context
                        that is resent every turn, so a 12k-char file read four
                        times costs ~36k tokens of pure duplication.

  2. REJECTION LOOPS  — when the submit_diagnosis grounding gate rejects, does
                        the agent actually change what it does next? A gate
                        that emits byte-identical rejections while the agent
                        repeats the same response is feedback that isn't
                        landing, and that is a harness defect, not a model one.

  3. BATCH HEADROOM   — how many turns could be saved if independent tool calls
                        were issued together? (base.py:372 parses one action
                        per turn, so N calls = N turns = N context resends.)

Why this exists: a single real trajectory (psf__requests-1142) showed the agent
submit -> get rejected -> re-read the identical 12,527-char file -> resubmit the
same defect, four times, until it exhausted its 15-iteration budget and
escalated. None of that is visible in anything previously persisted --
`agent_runs` keeps a tool-call count, logs/agent_sessions.jsonl keeps status
strings, and Langfuse truncates tool output to 500 chars (tracing.py:218).

Input: JSONL from `eval_swebench_diagnosis.py --steps-out`, which records full
untruncated observations. Langfuse is supported as a fallback but its 500-char
cap makes analyses 1 and 3 unreliable there -- see the warnings below.

    python scripts/eval_swebench_diagnosis.py --steps-out /tmp/steps.jsonl
    python scripts/analyze_trajectories.py --from-file /tmp/steps.jsonl
    python scripts/analyze_trajectories.py --from-file /tmp/steps.jsonl --verbose
    python scripts/analyze_trajectories.py --json > /tmp/trajectories.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TERMINAL_TOOLS = {"submit_diagnosis"}
_REJECTION_PREFIX = "REJECTED"

_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{3,}")
_STOPWORDS = {
    "true", "false", "none", "null", "error", "return", "self", "class", "def",
    "function", "const", "import", "from", "this", "that", "with", "value",
    "file", "path", "name", "type", "data", "line", "code", "test", "tests",
    "content", "source", "result", "results", "untrusted", "github", "python",
}


def _norm(value: Any) -> str:
    """Canonical form of a tool's arguments, for equality comparison."""
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return " ".join(value.split())
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return " ".join(str(value).split())


# ---------------------------------------------------------------------------
# 1. Repeated identical calls
# ---------------------------------------------------------------------------

def _repeats(calls: list[dict]) -> dict[str, Any]:
    seen: Counter[tuple[str, str]] = Counter()
    wasted_chars = 0
    detail: list[str] = []
    for c in calls:
        key = (c["name"], _norm(c["input"]))
        seen[key] += 1
        if seen[key] > 1:
            wasted_chars += len(c["output"] or "")
            detail.append(f"{c['name']}({_norm(c['input'])[:60]}) x{seen[key]}")
    repeated = {k: n for k, n in seen.items() if n > 1}
    return {
        "distinct_calls": len(seen),
        "total_calls": sum(seen.values()),
        "repeated_call_kinds": len(repeated),
        "redundant_calls": sum(n - 1 for n in repeated.values()),
        "wasted_output_chars": wasted_chars,
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# 2. Rejection loops
# ---------------------------------------------------------------------------

def _rejections(calls: list[dict]) -> dict[str, Any]:
    """Did each grounding rejection actually change the agent's behaviour?

    An "unproductive" rejection is one where BOTH the rejection text and the
    agent's next action are identical to the previous round. That is the
    signature of the gate saying the same thing, the agent doing the same
    thing, and nothing moving -- which burns the iteration budget.
    """
    rounds = []
    for i, c in enumerate(calls):
        out = (c["output"] or "").strip()
        if not out.startswith(_REJECTION_PREFIX):
            continue
        next_call = calls[i + 1] if i + 1 < len(calls) else None
        rounds.append({
            "iteration": c.get("iteration"),
            "message": out,
            "next_action": (next_call["name"] if next_call else None),
            "next_input": _norm(next_call["input"]) if next_call else None,
        })

    unproductive = 0
    identical_messages = 0
    for prev, cur in zip(rounds, rounds[1:]):
        same_msg = prev["message"] == cur["message"]
        same_response = (prev["next_action"], prev["next_input"]) == \
                        (cur["next_action"], cur["next_input"])
        identical_messages += int(same_msg)
        unproductive += int(same_msg and same_response)

    return {
        "rejections": len(rounds),
        "identical_repeat_messages": identical_messages,
        "unproductive_rounds": unproductive,
        "rounds": rounds,
    }


# ---------------------------------------------------------------------------
# 3. Batch headroom (upper bound -- see caveats in the report)
# ---------------------------------------------------------------------------

def _arg_tokens(tool_input: Any) -> set[str]:
    blob = _norm(tool_input)
    return {t for t in _TOKEN_RE.findall(blob)
            if t.lower() not in _STOPWORDS and not t.isdigit()}


def _batch(calls: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_outputs: list[str] = []
    for call in calls:
        if call["name"] in _TERMINAL_TOOLS:
            if current:
                groups.append(current)
            groups.append([call])
            current, current_outputs = [], []
            continue
        dep = None
        if current:
            haystack = "\n".join(current_outputs)
            for tok in sorted(_arg_tokens(call["input"]), key=len, reverse=True):
                if tok in haystack:
                    dep = tok
                    break
        if dep is not None:
            groups.append(current)
            current, current_outputs = [call], [call["output"] or ""]
        else:
            current.append(call)
            current_outputs.append(call["output"] or "")
    if current:
        groups.append(current)
    return groups


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _load_file(path: Path) -> dict[str, list[dict]]:
    traces: dict[str, list[dict]] = defaultdict(list)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        traces[rec["trace_id"]].append({
            "name": rec.get("name"),
            "iteration": rec.get("iteration"),
            "input": rec.get("input"),
            "output": rec.get("output") or "",
            "truncated": rec.get("output_truncated", False),
        })
    for calls in traces.values():
        calls.sort(key=lambda c: (c["iteration"] is None, c["iteration"]))
    return traces


def _fetch_langfuse(limit: int) -> dict[str, list[dict]]:
    """Fallback source. Outputs are capped at 500 chars by tracing.py:218, so
    repeat-waste totals and dependency detection are both degraded here."""
    from langfuse import Langfuse

    from app.core.config import settings

    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        raise SystemExit("Langfuse keys not configured — use --from-file.")

    client = Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_base_url,
    )

    def _page(**kwargs) -> list[Any]:
        out, cursor = [], None
        while len(out) < limit:
            batch = client.api.observations.get_many(
                type="TOOL", limit=min(100, limit - len(out)), cursor=cursor, **kwargs)
            if not batch.data:
                break
            out.extend(batch.data)
            cursor = getattr(getattr(batch, "meta", None), "cursor", None)
            if not cursor:
                if len(out) < limit:
                    print(f"  [note] Langfuse returned {len(out)} observations, no further "
                          f"cursor (asked {limit}) — that is the whole set.", file=sys.stderr)
                break
        return out

    meta = {o.id: o for o in _page()}
    io = {o.id: o for o in _page(fields="core,io")}
    traces: dict[str, list[dict]] = defaultdict(list)
    for obs_id, m in meta.items():
        if not m.trace_id:
            continue
        pair = io.get(obs_id)
        traces[m.trace_id].append({
            "name": m.name, "iteration": None, "start": m.start_time,
            "input": pair.input if pair else None,
            "output": (pair.output if pair else None) or "",
            "truncated": True,
        })
    for calls in traces.values():
        calls.sort(key=lambda c: (c.get("start") is None, c.get("start")))
    return traces


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _analyse(traces: dict[str, list[dict]], min_calls: int) -> dict[str, Any]:
    runs = []
    for trace_id, calls in traces.items():
        if len(calls) < min_calls:
            continue
        groups = _batch([dict(c) for c in calls])
        runs.append({
            "trace_id": trace_id,
            "calls": len(calls),
            "repeats": _repeats(calls),
            "rejections": _rejections(calls),
            "turns_now": len(calls),
            "turns_batched": len(groups),
            "turns_saved": len(calls) - len(groups),
        })

    def _tot(path: str, key: str) -> int:
        return sum(r[path][key] for r in runs)

    total_calls = sum(r["calls"] for r in runs)
    return {
        "runs": sorted(runs, key=lambda r: r["repeats"]["redundant_calls"], reverse=True),
        "runs_analysed": len(runs),
        "total_calls": total_calls,
        "redundant_calls": _tot("repeats", "redundant_calls"),
        "wasted_output_chars": _tot("repeats", "wasted_output_chars"),
        "rejections": _tot("rejections", "rejections"),
        "unproductive_rounds": _tot("rejections", "unproductive_rounds"),
        "identical_repeat_messages": _tot("rejections", "identical_repeat_messages"),
        "turns_now": sum(r["turns_now"] for r in runs),
        "turns_batched": sum(r["turns_batched"] for r in runs),
        "any_truncated": any(c.get("truncated") for cs in traces.values() for c in cs),
    }


def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "—"


def _print_report(res: dict[str, Any], verbose: bool) -> None:
    n = res["runs_analysed"]
    print(f"\n# DiagnosisAgent trajectory analysis (N={n} runs, {res['total_calls']} tool calls)\n")
    if not n:
        print("No runs matched. Lower --min-calls or check the input file.")
        return

    print("## 1. Repeated identical tool calls")
    print(f"  Redundant calls (same tool, same args) : {res['redundant_calls']}"
          f"  ({_pct(res['redundant_calls'], res['total_calls'])} of all calls)")
    print(f"  Duplicated observation text            : {res['wasted_output_chars']:,} chars"
          f"  (~{res['wasted_output_chars'] // 4:,} tokens, resent every later turn)")
    print("  Fix: memoise (tool, args) within a run. No API-semantics change, no")
    print("  base.py restructuring — a dict lookup before dispatch.\n")

    print("## 2. Grounding-gate rejection loops")
    rj, up = res["rejections"], res["unproductive_rounds"]
    print(f"  Rejections                             : {rj}")
    print(f"  Byte-identical repeat messages         : {res['identical_repeat_messages']}")
    print(f"  Unproductive rounds (same message AND  : {up}  ({_pct(up, rj)} of rejections)")
    print("    same next action as the round before)")
    if up:
        print("  The gate is the only real corrective signal in the loop; where this")
        print("  number is high it is not correcting anything, just burning budget.")
        print("  Fix: rewrite rejection messages — a Phase 3b-shaped intervention.\n")
    else:
        print("  No loops detected in this sample.\n")

    print("## 3. Batch headroom (UPPER BOUND)")
    tn, tb = res["turns_now"], res["turns_batched"]
    print(f"  Turns today / if batched               : {tn} / {tb}"
          f"  ({_pct(tn - tb, tn)} fewer)")
    print("  Independence is inferred by looking for a call's arguments in earlier")
    print("  outputs, and grouping is greedy — both err toward over-batching, so")
    print("  treat this as a ceiling. Compare against §1 before acting: deduplication")
    print("  is usually the cheaper win.\n")

    if res["any_truncated"]:
        print("!! Some observations were truncated in this input. §1's wasted-char total")
        print("!! is understated and §3's dependency detection has false negatives.\n")

    if verbose:
        print("Per-run detail (worst duplication first):")
        for r in res["runs"][:10]:
            rp, rj_ = r["repeats"], r["rejections"]
            print(f"\n  {r['trace_id']}")
            print(f"    calls={r['calls']}  redundant={rp['redundant_calls']}  "
                  f"wasted={rp['wasted_output_chars']:,}ch  "
                  f"rejections={rj_['rejections']} (unproductive {rj_['unproductive_rounds']})  "
                  f"turns {r['turns_now']}->{r['turns_batched']}")
            for d in rp["detail"][:4]:
                print(f"      repeat: {d}")
            for rd in rj_["rounds"][:2]:
                first = rd["message"].splitlines()[1] if "\n" in rd["message"] else rd["message"]
                print(f"      reject@{rd['iteration']} -> next={rd['next_action']}"
                      f" | {first[:88]}")


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from-file", type=Path, default=None,
                   help="JSONL from eval_swebench_diagnosis.py --steps-out (preferred)")
    p.add_argument("--limit", type=int, default=500,
                   help="Max Langfuse observations when --from-file is not given")
    p.add_argument("--min-calls", type=int, default=2, help="Ignore runs below this call count")
    p.add_argument("--json", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    traces = _load_file(args.from_file) if args.from_file else _fetch_langfuse(args.limit)
    res = _analyse(traces, args.min_calls)
    if args.json:
        print(json.dumps(res, indent=2, default=str))
    else:
        _print_report(res, args.verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
