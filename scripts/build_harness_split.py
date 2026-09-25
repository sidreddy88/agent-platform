"""
Build the evolve / held-out split for harness evolution on DiagnosisAgent.

Reads per-case gate history (app/evals/diagnosis_gate_history.json: every
attempt of every case across four CI runs) and writes the split to
app/evals/harness_split.json. Deterministic: same history in, same split out.
tests/test_harness_split.py asserts the committed file matches this output,
so the split can't drift silently once optimization starts.

Design, and why:

- **Held out by repo, not by case.** Every case from HELDOUT_REPOS is held
  out, and the optimizer never sees a trajectory from those repos. A
  per-case split would let an edit that only helps through knowledge of one
  repo's layout (e.g. "sphinx's config lives in X") score on held-out cases
  from the same repo. RRSI's leakage litmus test is "would this change still
  help on an unfamiliar task"; a repo the optimizer has never seen is the
  closest in-benchmark version of that.

- **Evolve on the cases that fail at least sometimes.** Two gate runs on
  unchanged code agreed on only 2 failures while 16 cases failed at least
  once: failures rotate. A case that has never failed in ~4 attempts gives
  the optimizer no signal to learn from. Every evolve-repo case that failed
  at least once is in the evolve set.

- **Plus a few always-pass cases as regression guards,** so an edit that
  breaks the easy path is caught during evolution, not only at the end. The
  cheapest one per distinct evolve repo, which keeps each candidate
  evaluation affordable.

- **Held-out keeps its failing cases too.** Held-out made only of
  always-pass cases would have nothing to improve on, so the final
  comparison couldn't show a gain. HELDOUT_REPOS was picked so that
  held-out gets a comparable share of the failing cases (6 of 19).

- **Out-of-distribution:** the 6 real production incidents
  (app/evals/diagnosis_regression.jsonl: a JS app, from CloudWatch). They're
  gitignored real data, so only their count is recorded here; they run
  locally.

Usage:
    python scripts/build_harness_split.py          # rewrite harness_split.json
    python scripts/build_harness_split.py --check  # exit 1 if it would change
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "app" / "evals" / "diagnosis_gate_history.json"
SAMPLE = ROOT / "app" / "evals" / "swebench_verified_sample.jsonl"
SPLIT = ROOT / "app" / "evals" / "harness_split.json"

HELDOUT_REPOS = ("pydata/xarray", "sphinx-doc/sphinx")
GUARDS_PER_EVOLVE = 4
# Excluded from every set, with the reason recorded in the output.
EXCLUDE = {
    "sympy__sympy-13091": "triggered the worktree-cleanup hang twice in CI (fixed in #258); "
                          "kept out until a clean run confirms the fix",
}
# Attempts that say nothing about the agent: provider billing failures and
# harness hangs.
NOT_AGENT = {"BILLING", "INFRA", "TIMEOUT"}


def _agent_attempts(case: dict) -> list[str]:
    return [v for attempts in case["attempts"].values() for v in attempts if v not in NOT_AGENT]


def build(history: dict, sample: list[dict] | None = None) -> dict:
    """`sample`: the 100-instance SWE-bench Verified sample. Its cases outside
    the 56-case gate set failed the original baseline (that's how the gate
    set was chosen), so they're the "hard" tier: added 2026-09-25, when round
    0 on Sonnet 5 showed only ~4 of the 17 original evolve cases still fail,
    too little signal for the optimizer."""
    cases = history["cases"]
    stats = {}
    for cid, c in cases.items():
        att = _agent_attempts(c)
        fails = [v for v in att if v != "PASS"]
        stats[cid] = {"repo": c["repo"], "agent_attempts": len(att), "fails": len(fails),
                      "cost_usd_run4": c.get("cost_usd_run4"), "source": "gate_history"}
    hard = sorted(inst["instance_id"] for inst in (sample or []) if inst["instance_id"] not in cases)
    for inst in sample or []:
        if inst["instance_id"] in cases:
            continue
        stats[inst["instance_id"]] = {"repo": inst["repo"], "agent_attempts": 1, "fails": 1,
                                      "cost_usd_run4": None, "source": "baseline_100_fail"}

    usable = {cid for cid in stats if cid not in EXCLUDE}
    heldout = sorted(cid for cid in usable if stats[cid]["repo"] in HELDOUT_REPOS)
    evolve_pool = sorted(cid for cid in usable if stats[cid]["repo"] not in HELDOUT_REPOS)
    hard_set = set(hard)

    evolve_failing = sorted(cid for cid in evolve_pool if stats[cid]["fails"] > 0 and cid not in hard_set)
    evolve_hard = sorted(cid for cid in evolve_pool if cid in hard_set)
    stable = [cid for cid in evolve_pool if stats[cid]["fails"] == 0]
    guards, seen_repos = [], set()
    for cid in sorted(stable, key=lambda c: (stats[c]["cost_usd_run4"] or 1e9, c)):
        if stats[cid]["repo"] in seen_repos:
            continue
        guards.append(cid)
        seen_repos.add(stats[cid]["repo"])
        if len(guards) == GUARDS_PER_EVOLVE:
            break
    reserve = sorted(set(stable) - set(guards))

    def est(ids):
        return round(sum(stats[c]["cost_usd_run4"] or 0 for c in ids), 2)

    return {
        "_doc": ("Evolve / held-out split for harness evolution on DiagnosisAgent. Generated "
                 "by scripts/build_harness_split.py from diagnosis_gate_history.json; do not "
                 "edit by hand. See the builder's docstring for the design."),
        "heldout_repos": list(HELDOUT_REPOS),
        "evolve": {"failing": evolve_failing, "guards": sorted(guards), "hard": evolve_hard},
        "heldout": {
            "failing": sorted(c for c in heldout if stats[c]["fails"] > 0 and c not in hard_set),
            "stable": sorted(c for c in heldout if stats[c]["fails"] == 0),
            "hard": sorted(c for c in heldout if c in hard_set),
        },
        "ood": {"source": "app/evals/diagnosis_regression.jsonl", "count": 6,
                "note": "real production incidents; gitignored, run locally"},
        "reserve": reserve,
        "excluded": dict(sorted(EXCLUDE.items())),
        "estimated_cost_usd_uncached": {
            "evolve_eval_k1": est(evolve_failing + guards),       # hard tier unmeasured
            "heldout_eval_k1": est(heldout),
        },
        "case_stats": dict(sorted(stats.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if the committed split differs from a fresh build")
    args = parser.parse_args()
    sample = [json.loads(line) for line in SAMPLE.read_text().splitlines() if line.strip()]
    split = build(json.loads(HISTORY.read_text()), sample)
    rendered = json.dumps(split, indent=1) + "\n"
    if args.check:
        if not SPLIT.exists() or SPLIT.read_text() != rendered:
            print("harness_split.json is out of date; run scripts/build_harness_split.py")
            return 1
        print("harness_split.json is up to date")
        return 0
    SPLIT.write_text(rendered)
    e, h = split["evolve"], split["heldout"]
    print(f"evolve: {len(e['failing'])} failing + {len(e['guards'])} guards + {len(e['hard'])} hard "
          f"(~${split['estimated_cost_usd_uncached']['evolve_eval_k1']} per k=1 eval, uncached)")
    print(f"heldout: {len(h['failing'])} failing + {len(h['stable'])} stable + {len(h['hard'])} hard from {HELDOUT_REPOS} "
          f"(~${split['estimated_cost_usd_uncached']['heldout_eval_k1']} per k=1 eval, uncached)")
    print(f"ood: {split['ood']['count']} production incidents | reserve: {len(split['reserve'])} "
          f"| excluded: {len(split['excluded'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
