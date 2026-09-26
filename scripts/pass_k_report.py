"""
pass@k / pass^k for DiagnosisAgent from a harness-optimizer run's trials.

    python scripts/pass_k_report.py runs/harness/r2-20260925
    python scripts/pass_k_report.py runs/harness/r2-20260925 --json

Reads every cached case result (evals/<harness hash>/k<trials>/<case>.json),
groups by harness, and reports per harness. Uses only trials already paid
for; add trials (a run with more calibration trials) to estimate higher k.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.evals.pass_k import summarize  # noqa: E402


def collect(run_dir: Path) -> dict[str, dict[str, tuple[int, int]]]:
    """harness hash -> case -> (trials, passes), merging a case's trial sets."""
    by_harness: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for f in sorted((run_dir / "evals").glob("*/k*/*.json")):
        harness = f.parent.parent.name
        case = json.loads(f.read_text())["case"]
        acc = by_harness[harness][f.stem]
        acc[0] += case["trials"]
        acc[1] += case["passes"]
    return {h: {c: (n, p) for c, (n, p) in cases.items()} for h, cases in by_harness.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = {h: summarize(t) for h, t in collect(args.run_dir).items()}
    if args.json:
        print(json.dumps(report, indent=1))
        return 0
    for h, r in report.items():
        print(f"harness {h}: {r['cases']} cases, >= {r['min_trials']} trials each")
        for k in (1, 2, 3):
            a, b = r.get(f"pass@{k}"), r.get(f"pass^{k}")
            if a is None:
                print(f"  k={k}: not estimable (need >= {k} trials per case)")
            else:
                print(f"  pass@{k} = {a:.3f}   pass^{k} = {b:.3f}")
        print(f"  flaky cases ({len(r['flaky_cases'])}): {', '.join(r['flaky_cases']) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
