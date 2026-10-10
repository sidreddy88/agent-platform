#!/usr/bin/env python
"""
Grade a Harbor SWE-bench job with the official harness and compare it with
one of our runs, case by case.

Harbor's verifier saves each agent's patch to verifier/model.patch (see
build_harbor_swebench.py); this collects them into predictions.jsonl, grades
them with the official SWE-bench harness on Modal (same path as our own runs),
and reports resolved counts, the paired split against our run, and how often
Harbor's own verifier agreed with the official grade.

    python scripts/grade_harbor_baseline.py --job runs/harbor/jobs-all500/<stamp> \\
        --out runs/harbor/all500 --ours all500-run2
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ours_resolved(run: str) -> set[str]:
    out = set()
    for f in (ROOT / "runs" / "fix" / run / "grading").rglob("report.json"):
        iid, r = next(iter(json.loads(f.read_text()).items()))
        if r.get("resolved"):
            out.add(iid)
    return out


def _sign_p(a: int, b: int) -> float:
    n, k = a + b, min(a, b)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n) if n else 1.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--ours", default="all500-run2")
    ap.add_argument("--model-name", default="mini-swe-agent")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    harbor, preds, cost, errors = {}, [], 0.0, []
    for f in sorted(args.job.glob("*/result.json")):
        d = json.loads(f.read_text())
        name = f.parent.name.rsplit("__", 1)[0]
        harbor[name] = ((d.get("verifier_result") or {}).get("rewards") or {}).get("reward") == 1.0
        cost += (d.get("agent_result") or {}).get("cost_usd") or 0.0
        if (d.get("exception_info") or {}).get("exception_type"):
            errors.append((name, d["exception_info"]["exception_type"]))
        patch = f.parent / "verifier" / "model.patch"
        if patch.exists() and patch.read_text().strip():
            preds.append({"instance_id": name, "model_name_or_path": args.model_name,
                          "model_patch": patch.read_text()})
    (args.out / "predictions.jsonl").write_text("".join(json.dumps(p) + "\n" for p in preds))
    print(f"trials {len(harbor)} | harbor-resolved {sum(harbor.values())} | patches {len(preds)} | "
          f"agent cost ${cost:.2f} | errors {len(errors)} {errors[:10]}", flush=True)

    cache = args.out / "resolved.json"
    if cache.exists():
        res = json.loads(cache.read_text())
    else:
        from app.harness_optimizer.fix_evaluator import modal_grade
        res = modal_grade(preds, run_id=f"{args.out.name}-grade", workdir=args.out / "grading")
        cache.write_text(json.dumps(res, indent=1))

    theirs = {i for i, v in res.items() if v}
    ours = _ours_resolved(args.ours)
    only_t, only_o = sorted(theirs - ours), sorted(ours - theirs)
    summary = {"cases": len(harbor), "theirs_resolved": len(theirs), "ours_resolved": len(ours),
               "both": len(theirs & ours), "theirs_only": only_t, "ours_only": only_o,
               "sign_test_p": round(_sign_p(len(only_t), len(only_o)), 6),
               "harbor_official_agreement": sum(1 for i in harbor if harbor[i] == bool(res.get(i))),
               "agent_cost_usd": round(cost, 2), "errors": errors}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(f"OFFICIAL: {args.model_name} {len(theirs)}/500 | ours ({args.ours}) {len(ours)}/500 | "
          f"both {len(theirs & ours)} | theirs-only {len(only_t)} | ours-only {len(only_o)} | "
          f"sign test p {summary['sign_test_p']} | harbor/official agree "
          f"{summary['harbor_official_agreement']}/{len(harbor)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
