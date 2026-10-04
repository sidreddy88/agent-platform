"""
Grade a fix-eval predictions file with the official SWE-bench harness, in
batches small enough for a laptop's disk.

SWE-bench x86 images are ~4–8 GB each (shared layers bring that down, but 66+
images still don't fit a Colima disk). Per batch: pull the batch's images with
--platform linux/amd64 (the harness's own pull fails on Apple silicon), run
scripts/swebench_eval_x86.py on just those instances, then delete the images.
Stops before a batch if the Mac's free disk is under --min-free-gb.

Resumable: a batch whose harness report already exists is skipped. The combined
result goes to <run_dir>/grade_summary.json.

Usage (Colima running at 4 CPUs / 6 GB; ~/.venvs/swebench has swebench 4.0.3):
    DOCKER_CONTEXT=colima python scripts/grade_swebench_batched.py runs/fix/heldout66 --batch 10
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HARNESS_PY = Path.home() / ".venvs" / "swebench" / "bin" / "python"
MODEL_NAME = "remediate-labs-diagnosis+fixgen"


def image_for(instance_id: str) -> str:
    return f"swebench/sweb.eval.x86_64.{instance_id.replace('__', '_1776_')}:latest"


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def free_gb(path: Path) -> float:
    return shutil.disk_usage(path).free / 1e9


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", type=Path, help="a fix-eval run dir with predictions.jsonl")
    ap.add_argument("--batch", type=int, default=10, help="instances per batch")
    ap.add_argument("--workers", type=int, default=2, help="harness --max_workers")
    ap.add_argument("--min-free-gb", type=float, default=30.0, help="stop if the Mac has less free")
    ap.add_argument("--keep-images", action="store_true", help="don't delete images after a batch")
    args = ap.parse_args()

    run_dir = args.run_dir.resolve()
    preds = [json.loads(line) for line in (run_dir / "predictions.jsonl").read_text().splitlines() if line]
    ids = sorted(p["instance_id"] for p in preds)
    batches = [ids[i:i + args.batch] for i in range(0, len(ids), args.batch)]
    work = run_dir / "grading"
    work.mkdir(exist_ok=True)
    resolved: set[str] = set()
    unresolved: set[str] = set()
    errors: set[str] = set()

    for n, batch in enumerate(batches, 1):
        run_id = f"{run_dir.name}-b{n:02d}"
        report = work / f"{MODEL_NAME}.{run_id}.json"
        if not report.exists():
            if free_gb(Path.home()) < args.min_free_gb:
                print(f"stopping before batch {n}: {free_gb(Path.home()):.0f} GB free < {args.min_free_gb}")
                break
            print(f"batch {n}/{len(batches)}: {len(batch)} instance(s), pulling images", flush=True)
            for i in batch:
                if docker("image", "inspect", image_for(i), check=False).returncode != 0:
                    docker("pull", "-q", "--platform", "linux/amd64", image_for(i))
            sub = work / f"{run_id}.predictions.jsonl"
            sub.write_text("".join(json.dumps(p) + "\n" for p in preds if p["instance_id"] in batch))
            print(f"batch {n}: grading", flush=True)
            subprocess.run([str(HARNESS_PY), str(ROOT / "scripts" / "swebench_eval_x86.py"),
                            "--dataset_name", "princeton-nlp/SWE-bench_Verified",
                            "--predictions_path", str(sub), "--run_id", run_id,
                            "--namespace", "swebench", "--max_workers", str(args.workers),
                            "--cache_level", "instance"],
                           cwd=work, stdout=(work / f"{run_id}.log").open("w"), stderr=subprocess.STDOUT)
            if not args.keep_images:
                for i in batch:
                    docker("rmi", "-f", image_for(i), check=False)
                docker("image", "prune", "-f", check=False)
        data = json.loads(report.read_text()) if report.exists() else {}
        resolved |= set(data.get("resolved_ids", []))
        unresolved |= set(data.get("unresolved_ids", []))
        errors |= set(data.get("error_ids", []))
        print(f"batch {n}: resolved {len(set(data.get('resolved_ids', [])))}/{len(batch)}", flush=True)

    summary = {"graded": len(resolved) + len(unresolved) + len(errors), "predictions": len(ids),
               "resolved": sorted(resolved), "unresolved": sorted(unresolved), "errors": sorted(errors)}
    (run_dir / "grade_summary.json").write_text(json.dumps(summary, indent=2))
    print(f"resolved {len(resolved)} / {len(ids)} predictions "
          f"({len(unresolved)} unresolved, {len(errors)} errors)")
    return 0


if __name__ == "__main__":
    if not os.environ.get("DOCKER_CONTEXT"):
        print("set DOCKER_CONTEXT (e.g. colima)", file=sys.stderr)
    sys.exit(main())
