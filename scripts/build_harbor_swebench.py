#!/usr/bin/env python
"""
Prepare Harbor's SWE-bench Verified tasks for a fair same-model baseline
(mini-swe-agent vs our pipeline on DeepSeek-V4.1-Flash).

Starts from Harbor's own task set (`harbor datasets download swebench-verified`)
and changes two things, both so the agent sees what our fix agent sees:

  1. Network: only the model API is reachable while the agent works
     (network_mode = "allowlist" under [agent]). With open internet,
     mini-swe-agent looked the answer up in 14 of 20 trials (GitHub issues and
     fix commits, newer releases from PyPI; localization pilot, 2026-10-01).
  2. Caches: the image's pip/uv/conda package caches are removed, as our test
     sandbox does (app/services/test_sandbox.py). With the network blocked, the
     agent hunted newer releases in those caches instead.

Everything else (instruction, image, tests, timeouts) is Harbor's.

    harbor datasets download swebench-verified -o /tmp/hb
    python scripts/build_harbor_swebench.py --src /tmp/hb/swebench-verified \\
        --out runs/harbor/tasks-pilot20 --sample 20
    python scripts/build_harbor_swebench.py --src ... --out ... --network-check   # oracle-only probe task
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FULL = ROOT / "app" / "evals" / "swebench_verified_full.jsonl"
ALLOWED = ["api.together.xyz", "api.together.ai"]
CLEAN_CACHES = ("RUN rm -rf /root/.cache/pip /root/.cache/uv /root/.cache/pip-tools "
                "/opt/miniconda3/pkgs/*/ 2>/dev/null; true")


def patch_task(task_dir: Path, allowed: list[str]) -> None:
    toml = task_dir / "task.toml"
    text = toml.read_text()
    block = ("# Only the model API is reachable while the agent works (see build_harbor_swebench.py).\n"
             f'network_mode = "allowlist"\nallowed_hosts = {json.dumps(allowed)}\n')
    if "network_mode" not in text:
        text = re.sub(r"(\[agent\]\n(?:[^\[]*\n)?)", lambda m: m.group(1) + block, text, count=1) \
            if "[agent]" in text else text + "\n[agent]\n" + block
    toml.write_text(text)
    dockerfile = task_dir / "environment" / "Dockerfile"
    d = dockerfile.read_text()
    if CLEAN_CACHES not in d:
        dockerfile.write_text(d.rstrip("\n") + "\n# Package caches can hold newer releases of the code under test.\n"
                              + CLEAN_CACHES + "\n")


def network_check_task(template: Path, out: Path, allowed: list[str]) -> Path:
    """A copy of one task whose oracle solution probes the network: Together must
    answer, GitHub and PyPI must not. Run with the oracle agent (no model calls)."""
    dest = out / "network-check"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(template, dest)
    patch_task(dest, allowed)
    (dest / "solution" / "solve.sh").write_text("""#!/bin/bash
probe() { if curl -s -m 10 -o /dev/null "$1"; then echo "REACHABLE $1"; else echo "BLOCKED $1"; fi; }
probe https://api.together.xyz/v1/models
probe https://api.together.ai/v1/models
probe https://github.com
probe https://pypi.org/simple/
ls /root/.cache/pip 2>/dev/null && echo "pip cache present" || echo "pip cache gone"
""")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="Harbor's downloaded swebench-verified directory")
    ap.add_argument("--out", required=True)
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--sample", type=int, help="random sample of N cases (seed 20261011)")
    group.add_argument("--cases", help="comma-separated instance ids")
    group.add_argument("--all", action="store_true")
    group.add_argument("--network-check", action="store_true")
    ap.add_argument("--allow-host", action="append", default=None)
    args = ap.parse_args()
    src, out = Path(args.src), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    allowed = args.allow_host or ALLOWED
    if args.network_check:
        print(network_check_task(next(p for p in sorted(src.iterdir()) if p.is_dir()), out, allowed))
        return 0
    ids = sorted(json.loads(x)["instance_id"] for x in FULL.read_text().splitlines() if x)
    if args.sample:
        ids = sorted(random.Random(20261011).sample(ids, args.sample))
    elif args.cases:
        ids = args.cases.split(",")
    for i in ids:
        dest = out / i
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src / i, dest)
        patch_task(dest, allowed)
    (out / "cases.json").write_text(json.dumps(ids, indent=1))
    print(f"{len(ids)} tasks in {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
