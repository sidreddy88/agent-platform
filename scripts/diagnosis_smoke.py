"""
Smoke tier in front of the diagnosis gate v2, measured with agentstats (github.com/sidreddy88/agentstats).

Gate v2 (app/evals/gate_v2.py) replays all 434 pool cases twice: ~$29 and ~40 minutes. A PR that simply breaks
diagnosis shouldn't need that to find out. The smoke tier replays a stratified sample of the pool (one trial,
~40 cases spread across repos in proportion, same seed every time) and compares each case with main's baseline
rate on it. It FAILs only when the PR is clearly worse than main by more than the smoke margin (breakage); a
PASS or an INCONCLUSIVE result lets the full gate run, which makes the real decision.

    python scripts/diagnosis_smoke.py sample --out runs/smoke/cases.json        # the cases file for the runner
    python scripts/optimize_harness.py --run-dir runs/smoke --cases-file runs/smoke/cases.json --rounds 0 \\
        --calibration-trials 1 --budget 3 --parallel 2 --lane-width 3
    python scripts/gate_v2.py collect --run-dir runs/smoke --trials 1 --out runs/smoke/verdicts.json
    python scripts/diagnosis_smoke.py convert --results runs/smoke/verdicts.json --out runs/smoke/pr.jsonl
    python scripts/diagnosis_smoke.py baseline --cases runs/smoke/cases.json --out runs/smoke/baseline.jsonl
    agentstats gate --policy app/evals/diagnosis_gate_policy.toml --tier smoke \\
        --candidate runs/smoke/pr.jsonl --baseline runs/smoke/baseline.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.evals import gate_v2  # noqa: E402

POLICY = ROOT / "app" / "evals" / "diagnosis_gate_policy.toml"


def _rows(verdicts: dict[str, list[str]], repos: dict[str, str], arm: str) -> list[dict]:
    """Gate v2 verdicts as agentstats rows. PASS/FAIL are graded; anything else (a replay that errored or was
    stopped by the runner) is an infrastructure failure, kept out of the pass rate."""
    out = []
    for case, vs in sorted(verdicts.items()):
        for trial, v in enumerate(vs, 1):
            graded = v in ("PASS", "FAIL")
            out.append({"arm": arm, "run": trial, "case": case, "passed": (v == "PASS") if graded else None,
                        "infra_error": None if graded else (v or "Missing"), "tags": [f"repo:{repos.get(case, '?')}"]})
    return out


def _write(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("convert")
    c.add_argument("--results", type=Path, nargs="+", required=True)
    c.add_argument("--out", type=Path, required=True)
    b = sub.add_parser("baseline")
    b.add_argument("--out", type=Path, required=True)
    b.add_argument("--cases", type=Path, help="the sample's cases file: keep only those cases")
    args = ap.parse_args()

    pool = gate_v2.load_pool()
    if args.cmd == "sample":
        from agentstats.sample import stratified

        tier = tomllib.loads(POLICY.read_text())["smoke"]
        cases = {c: [f"repo:{pool['repos'][c]}"] for c in pool["cases"]}
        picked = stratified(cases, int(tier["sample"]), tier.get("stratify_by"), tier.get("always_include"),
                            int(tier.get("seed", 0)))
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"evolve": picked["cases"], "guards": []}))
        print(f"{len(picked['cases'])} smoke cases across {len(picked['groups'])} repos: {picked['groups']}")
    elif args.cmd == "convert":
        verdicts = gate_v2.merge(*(json.loads(p.read_text())["verdicts"] for p in args.results))
        _write(_rows(verdicts, pool["repos"], "pr"), args.out)
        print(f"{len(verdicts)} cases -> {args.out}")
    elif args.cmd == "baseline":
        base = gate_v2.load_baseline()
        keep = set(json.loads(args.cases.read_text())["evolve"]) if args.cases else None
        verdicts = {c: v for c, v in base["cases"].items() if keep is None or c in keep}
        _write(_rows(verdicts, pool["repos"], "main"), args.out)
        print(f"baseline from main {base['main_sha'][:8]} ({base['trials']} trials) -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
