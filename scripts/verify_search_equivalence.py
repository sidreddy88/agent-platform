"""
Check that the git-grep prefilter (LocalRepoService.files_containing, used by
grep_codebase and the local symbol lookup) changes nothing but speed.

    python scripts/verify_search_equivalence.py --grep 150 --verify 60

Replays real grep_codebase and verify_symbol_in_repo calls recorded in
harness-optimizer trajectories against each case's checkout at its pinned
commit, once with the full scan (the old code path) and once with the
prefilter, and requires byte-identical tool output. Also reports the speedup.
No LLM calls.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def recorded_calls(runs: list[str]) -> dict[str, list[tuple[str, dict]]]:
    by_case: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for run in runs:
        for f in (ROOT / "runs" / "harness" / run / "evals").glob("*/k*/*.json"):
            for t in json.loads(f.read_text())["trajectories"]:
                for st in t.get("steps") or []:
                    if st["name"] in ("grep_codebase", "verify_symbol_in_repo"):
                        inp = st.get("input")
                        if isinstance(inp, str):
                            try:
                                inp = json.loads(inp)
                            except json.JSONDecodeError:
                                continue
                        if isinstance(inp, dict):
                            by_case[t["instance_id"]].append((st["name"], inp))
    return by_case


async def main_async(args) -> int:
    from app.agents import diagnosis
    from app.agents.diagnosis import DiagnosisAgent
    from app.harness_optimizer.evaluator import ReplayEvaluator
    from app.services.repo import LocalRepoService

    instances = ReplayEvaluator()._instances
    by_case = recorded_calls(args.runs)
    rng = random.Random(7)
    pool = [(case, name, inp) for case, calls in by_case.items() if case in instances
            for name, inp in calls]
    rng.shuffle(pool)
    greps = [c for c in pool if c[1] == "grep_codebase"][: args.grep]
    verifies = [c for c in pool if c[1] == "verify_symbol_in_repo"][: args.verify]
    chosen: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for case, name, inp in greps + verifies:
        chosen[case].append((name, inp))

    full_scan = lambda repo, text: sorted(repo.list_files())
    prefiltered = diagnosis._candidate_files
    mismatches, n, t_old, t_new = [], 0, 0.0, 0.0
    for case, calls in sorted(chosen.items()):
        inst = instances[case]
        owner, repo = inst["repo"].split("/", 1)
        local = LocalRepoService(owner, repo, pinned_sha=inst["base_commit"])
        await local.ensure_fresh()
        try:
            agent = DiagnosisAgent(local_repo=local, owner=owner, repo=repo)
            for name, inp in calls:
                fn = agent._tools[name][0]
                outs = []
                for impl in (full_scan, prefiltered):
                    diagnosis._candidate_files = impl
                    t0 = time.perf_counter()
                    outs.append(await fn(**inp))
                    dt = time.perf_counter() - t0
                    if impl is full_scan:
                        t_old += dt
                    else:
                        t_new += dt
                diagnosis._candidate_files = prefiltered
                n += 1
                if outs[0] != outs[1]:
                    mismatches.append({"case": case, "tool": name, "input": inp,
                                       "old": outs[0][:300], "new": outs[1][:300]})
        finally:
            await local.remove_worktree()
        print(f"{case}: {len(calls)} calls, mismatches so far {len(mismatches)}", flush=True)
    print(json.dumps({"calls": n, "cases": len(chosen), "mismatches": len(mismatches),
                      "old_seconds": round(t_old, 1), "new_seconds": round(t_new, 1),
                      "speedup": round(t_old / t_new, 1) if t_new else None}, indent=1))
    for m in mismatches[:5]:
        print(json.dumps(m, indent=1))
    return 1 if mismatches else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--grep", type=int, default=150)
    ap.add_argument("--verify", type=int, default=60)
    ap.add_argument("--runs", nargs="*", default=["r5", "r8-deepseek", "g500-deepseek"])
    args = ap.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
