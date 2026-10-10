#!/usr/bin/env python
"""
Does test mode's test selection find the tests a fix broke? No LLM calls.

For each case whose run-2 patch broke existing tests (PASS_TO_PASS failures in
the official grading report), start the case's SWE-bench image on Modal, pick
test files the way test mode does (app/services/repo_tests.py), and check
whether they include the files of the tests that broke. With --baseline, also
run the picked tests on the unchanged repo (command, parsing and timing check).

    python scripts/check_test_selection.py --cases-file /tmp/broke36.json --parallel 6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services import repo_tests as rt  # noqa: E402
from app.services.test_sandbox import ModalTestSandbox, swebench_image  # noqa: E402

REPORTS = ROOT / "runs/fix/all500-run2/grading/logs/run_evaluation/all500-run2"


def broken_tests(instance_id: str) -> list[str]:
    report = next(REPORTS.glob(f"*/{instance_id}/report.json"))
    status = json.loads(report.read_text())[instance_id]["tests_status"]
    return status["PASS_TO_PASS"]["failure"]


def covered(test_id: str, files: list[str], framework: str) -> bool | None:
    """Is a broken test in one of the picked files? None if the id has no file (sympy)."""
    if framework == "pytest":
        return test_id.split("::")[0] in files
    if framework == "django":
        inner = test_id[test_id.find("(") + 1:test_id.rfind(")")] if "(" in test_id else test_id
        labels = [lbl for lbl in (rt.django_label(f) for f in files) if lbl]
        return any(inner == lbl or inner.startswith(lbl + ".") for lbl in labels)
    return None


async def one(case: dict, gate: asyncio.Semaphore, baseline: bool, max_files: int, timeout: int) -> dict:
    iid = case["id"]
    framework = rt.framework_for(iid.split("__")[0] + "/" + iid.split("__")[1].rsplit("-", 1)[0])
    out = {"id": iid, "framework": framework, "file": case["file"], "function": case["function"]}
    async with gate:
        t0 = time.monotonic()
        sb = None
        try:
            sb = await ModalTestSandbox(swebench_image(iid), lifetime=60 * 20).start()
            files = await rt.find_test_files(sb, case["file"] or "", case["function"], max_files)
            out["picked"] = files
            broken = broken_tests(iid)
            hits = [covered(t, files, framework) for t in broken]
            out["broken"] = len(broken)
            out["covered"] = None if all(h is None for h in hits) else sum(1 for h in hits if h)
            if baseline and files:
                run = await rt.run_tests(sb, framework, files, timeout)
                out["baseline"] = run.summary("before")
        except Exception as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            if sb is not None:
                await sb.close()
        out["seconds"] = round(time.monotonic() - t0, 1)
    print(json.dumps({k: out.get(k) for k in ("id", "covered", "broken", "picked", "error")}), flush=True)
    return out


async def main_async(cases: list[dict], parallel: int, baseline: bool, max_files: int,
                     timeout: int, out_path: Path) -> None:
    gate = asyncio.Semaphore(parallel)
    results = await asyncio.gather(*(one(c, gate, baseline, max_files, timeout) for c in cases))
    out_path.write_text(json.dumps(results, indent=1))
    known = [r for r in results if r.get("covered") is not None]
    any_hit = sum(1 for r in known if r["covered"])
    print(f"\n{len(results)} cases; {len(known)} with file-level test ids; "
          f"selection covers at least one broken test in {any_hit}/{len(known)}; "
          f"errors: {sum(1 for r in results if r.get('error'))}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases-file", required=True)
    ap.add_argument("--only", help="comma-separated ids from the file")
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--max-files", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--out", default="/tmp/test_selection_check.json")
    args = ap.parse_args()
    cases = json.loads(Path(args.cases_file).read_text())
    if args.only:
        keep = set(args.only.split(","))
        cases = [c for c in cases if c["id"] in keep]
    asyncio.run(main_async(cases, args.parallel, args.baseline, args.max_files, args.timeout,
                           Path(args.out)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
