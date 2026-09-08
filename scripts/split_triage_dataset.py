"""
Stratified train/held-out split for the TriageAgent golden dataset
(app/evals/triage_synthetic_dataset.jsonl -- 532 synthetic-balanced +
4 real-cloudwatch cases).

Stratified by category (P0/P1/P2/P3/noise/duplicate), not a flat random
split -- a flat split could easily leave the held-out set with zero P0
examples by chance, given P0 is already the smallest category (53 of
536). Splitting within each category separately guarantees every
category is proportionally represented in both halves.

Fixed random seed, chosen before looking at the split results (same
discipline as the SWE-bench sampling this session) -- no re-rolling
until the split "looks good."

Usage:
    python scripts/split_triage_dataset.py
    python scripts/split_triage_dataset.py --train-frac 0.8 --seed 42
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

_IN_PATH = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_synthetic_dataset.jsonl"
_TRAIN_PATH = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_train.jsonl"
_HELDOUT_PATH = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_heldout.jsonl"


def _category(case: dict[str, Any]) -> str:
    """P0-P3 cases are keyed by intended_category (severity target);
    noise/duplicate cases are keyed the same way. Real-cloudwatch cases
    have no intended_category (no predetermined target) -- fall back to
    the actual output severity for those."""
    return case["metadata"].get("intended_category") or case["output"]["severity"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--train-frac", type=float, default=0.8)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    with _IN_PATH.open() as f:
        cases = [json.loads(line) for line in f]

    by_category: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_category[_category(case)].append(case)

    rng = random.Random(args.seed)
    train: list[dict[str, Any]] = []
    heldout: list[dict[str, Any]] = []

    print(f"Stratified split (train_frac={args.train_frac}, seed={args.seed}):\n")
    for cat in sorted(by_category):
        group = by_category[cat][:]
        rng.shuffle(group)
        split_idx = round(len(group) * args.train_frac)
        train.extend(group[:split_idx])
        heldout.extend(group[split_idx:])
        print(f"  {cat:<10} total={len(group):>4}  train={split_idx:>4}  heldout={len(group)-split_idx:>4}")

    rng.shuffle(train)
    rng.shuffle(heldout)

    with _TRAIN_PATH.open("w") as f:
        for case in train:
            f.write(json.dumps(case) + "\n")
    with _HELDOUT_PATH.open("w") as f:
        for case in heldout:
            f.write(json.dumps(case) + "\n")

    print(f"\nTotal: {len(cases)} -> train={len(train)}, heldout={len(heldout)}")
    print(f"Written to {_TRAIN_PATH.name} and {_HELDOUT_PATH.name}")

    # Sanity check: no case appears in both halves.
    train_ids = {c["id"] for c in train}
    heldout_ids = {c["id"] for c in heldout}
    overlap = train_ids & heldout_ids
    print(f"Overlap check: {len(overlap)} case(s) in both halves (should be 0)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
