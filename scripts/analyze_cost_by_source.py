"""
Where does DiagnosisAgent's money go? Cost by prompt source, from trajectories.

Input: the per-attempt trajectory records written by
`scripts/eval_diagnosis_full_regression.py --trajectories-out DIR` (or any
list of records from `_replay_one(trajectory_sink=...)`). Each record holds,
for every ReAct request, the prompt's composition in order ([label, chars]
segments) and that request's measured usage (uncached input, cache writes,
cache reads, output) from the cost meter.

Attribution, per request:
  1. Scale each segment's chars to tokens so they sum to the request's
     measured input tokens (uncached + cache write + cache read). Chars are
     only used for proportions; the totals are measured, not estimated.
  2. Walk the prompt in order. Caching is a prefix match, so the oldest
     tokens are the ones served from cache: the first `cache_read` tokens are
     priced as cache reads, the next `cache_write` as cache writes, and the
     rest as uncached input. The static prefix (tool descriptions, system
     instructions) therefore lands mostly in cache reads, and the newest tool
     output in writes or uncached input, which is how it's actually billed.
  3. Output tokens are attributed to `model_output`.

Also reported: how many requests each observation was resent in (every
later turn carries it again), and cost per case split by verdict, since
failing cases use every turn.

Why this exists: the Cursor harness-efficiency post (2026-09) opens with
exactly this chart, spend by source x billing type, and the optimizer needs
it to know which part of the harness is worth editing. Our telemetry
(#256) split cost by billing type but not by source.

    python scripts/analyze_cost_by_source.py DIR
    python scripts/analyze_cost_by_source.py DIR --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.cost_meter import (  # noqa: E402
    CACHE_READ_MULTIPLIER,
    CACHE_WRITE_MULTIPLIER,
    PRICES_PER_MTOK,
    _price_key,
)

BILLING = ("cache_read", "cache_write", "uncached", "output")


def _rates(model: str) -> dict[str, float] | None:
    prices = PRICES_PER_MTOK.get(_price_key(model))
    if prices is None:
        return None
    inp, out = prices
    return {"uncached": inp / 1e6, "cache_write": inp * CACHE_WRITE_MULTIPLIER / 1e6,
            "cache_read": inp * CACHE_READ_MULTIPLIER / 1e6, "output": out / 1e6}


def attribute_call(segments: list, usage: list[dict]) -> dict[str, dict[str, float]]:
    """Tokens and cost per (label, billing type) for one request."""
    out: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for u in usage:
        rates = _rates(u["model"])
        if rates is None:
            out["_unpriced"]["tokens"] += u["input"] + u["cache_read"] + u["cache_write"] + u["output"]
            continue
        total_in = u["input"] + u["cache_read"] + u["cache_write"]
        total_chars = sum(c for _, c in segments)
        budget = {"cache_read": u["cache_read"], "cache_write": u["cache_write"], "uncached": u["input"]}
        if total_in and total_chars:
            scale = total_in / total_chars
            for label, chars in segments:
                remaining = chars * scale
                for kind in ("cache_read", "cache_write", "uncached"):
                    take = min(remaining, budget[kind])
                    if take > 0:
                        out[label][f"{kind}_tokens"] += take
                        out[label]["cost"] += take * rates[kind]
                        budget[kind] -= take
                        remaining -= take
                    if remaining <= 1e-9:
                        break
        out["model_output"]["output_tokens"] += u["output"]
        out["model_output"]["cost"] += u["output"] * rates["output"]
    return out


def analyze(records: list[dict]) -> dict:
    by_source: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    resends: dict[str, list[int]] = defaultdict(list)
    by_verdict: dict[str, list[float]] = defaultdict(list)
    attributed_total = recorded_total = 0.0
    for rec in records:
        calls = rec.get("llm_calls") or []
        case_cost = 0.0
        for call in calls:
            for label, vals in attribute_call(call["segments"], call["usage"]).items():
                for k, v in vals.items():
                    by_source[label][k] += v
                case_cost += vals.get("cost", 0.0)
        # An observation first appears in request i and is resent in every
        # later request of the same attempt.
        n = len(calls)
        seen = 0
        for i, call in enumerate(calls):
            obs = [lbl for lbl, _ in call["segments"] if lbl.startswith("observation:")]
            for lbl in obs[seen:]:
                resends[lbl].append(n - i)
            seen = len(obs)
        attributed_total += case_cost
        recorded_total += (rec.get("cost") or {}).get("cost_usd") or 0.0
        by_verdict[rec.get("verdict", "?")].append(case_cost)

    grand = sum(v.get("cost", 0.0) for v in by_source.values()) or 1.0
    rows = sorted(({"source": k, **{kk: round(vv, 6) for kk, vv in v.items()},
                    "share": round(v.get("cost", 0.0) / grand, 4)} for k, v in by_source.items()),
                  key=lambda r: -r.get("cost", 0.0))
    return {
        "records": len(records),
        "requests": sum(len(r.get("llm_calls") or []) for r in records),
        "attributed_cost_usd": round(attributed_total, 4),
        "recorded_cost_usd": round(recorded_total, 4),
        # Non-ReAct LLM calls (e.g. a judge) are in the meter but not in llm_calls.
        "unattributed_cost_usd": round(max(0.0, recorded_total - attributed_total), 4),
        "by_source": rows,
        "observation_resends": {k: {"observations": len(v), "mean_requests_carried": round(sum(v) / len(v), 2)}
                                for k, v in sorted(resends.items())},
        "cost_per_case_by_verdict": {k: {"cases": len(v), "mean_usd": round(sum(v) / len(v), 4)}
                                     for k, v in sorted(by_verdict.items())},
    }


def _load(path: Path) -> list[dict]:
    files = sorted(path.glob("*.json")) if path.is_dir() else [path]
    records = []
    for f in files:
        data = json.loads(f.read_text())
        records.extend(data if isinstance(data, list) else [data])
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("path", type=Path, help="trajectory record file or directory of them")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = analyze(_load(args.path))
    if args.json:
        print(json.dumps(report, indent=1))
        return 0
    print(f"# Cost by source ({report['records']} attempts, {report['requests']} requests)\n")
    print(f"attributed ${report['attributed_cost_usd']:.2f} of recorded ${report['recorded_cost_usd']:.2f} "
          f"(unattributed, non-ReAct calls: ${report['unattributed_cost_usd']:.2f})\n")
    print(f"{'source':34s} {'share':>6s} {'cost':>9s}  {'cache_read':>10s} {'cache_write':>11s} "
          f"{'uncached':>9s} {'output':>8s}  (tokens)")
    for r in report["by_source"]:
        print(f"{r['source']:34s} {r['share']:6.1%} ${r.get('cost', 0):8.3f}  "
              f"{r.get('cache_read_tokens', 0):10.0f} {r.get('cache_write_tokens', 0):11.0f} "
              f"{r.get('uncached_tokens', 0):9.0f} {r.get('output_tokens', 0):8.0f}")
    print("\nObservations: mean number of requests each is carried in")
    for k, v in report["observation_resends"].items():
        print(f"  {k:32s} {v['observations']:4d} observations, carried in {v['mean_requests_carried']:.1f} requests")
    print("\nCost per case by verdict")
    for k, v in report["cost_per_case_by_verdict"].items():
        print(f"  {k:10s} {v['cases']:3d} attempts, mean ${v['mean_usd']:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
