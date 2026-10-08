"""
Diagnosis + fix on SWE-bench Verified: does FixGenerationAgent write a fix
that passes the repo's own tests?

Per instance:
  1. Check out the repo at the bug's base commit (LocalRepoService, pinned).
  2. DiagnosisAgent diagnoses the issue, exactly as scripts/eval_swebench_diagnosis.py
     replays it, and is graded for localization the same way (_grade).
  3. If diagnosis named a file the real fix touched, FixGenerationAgent runs on
     that diagnosis with patch_only=True: target resolution, context, fix
     generation and the self-critique, but no sandbox, no Issue, no PR.
  4. The fixed file is turned into a git diff against the base commit and
     written as a SWE-bench prediction.

Grading is not done here: the predictions file goes to the official SWE-bench
harness, which applies each patch in the instance's Docker image and runs
FAIL_TO_PASS and PASS_TO_PASS tests:

    python -m swebench.harness.run_evaluation --dataset_name princeton-nlp/SWE-bench_Verified \\
        --predictions_path runs/fix/<run>/predictions.jsonl --run_id <run> --max_workers 2

The fix agent was built for one production repo, so this script points its
tools at the pinned checkout instead: file reads and code search go to the
local worktree (GitHub's default branch would show post-fix code), find_callers
uses a call graph built from the checkout, and every GitHub write raises.

Usage:
    python scripts/eval_swebench_fix.py --pilot --run fix-pilot
    python scripts/eval_swebench_fix.py --cases pydata__xarray-2905 --run smoke
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import faulthandler
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.eval_swebench_diagnosis import _grade, _touched_files  # noqa: E402

FULL = ROOT / "app" / "evals" / "swebench_verified_full.jsonl"
SPLIT = ROOT / "app" / "evals" / "harness_split.json"
OUT = ROOT / "runs" / "fix"
MODEL_NAME = "remediate-labs-diagnosis+fixgen"

# Held-out cases (xarray, sphinx) whose real fix touches one file and that the
# evolved diagnosis harness localized on 3 of 3 trials in r9: the fix agent is
# judged on fixing, not on finding. Sorted ids, alternating repos.
PILOT = [
    "pydata__xarray-2905", "sphinx-doc__sphinx-10323", "pydata__xarray-3151", "sphinx-doc__sphinx-10435",
    "pydata__xarray-4075", "sphinx-doc__sphinx-10449", "pydata__xarray-4094", "sphinx-doc__sphinx-10466",
    "pydata__xarray-4356", "sphinx-doc__sphinx-10614",
]


class LocalOnlyGitHub:
    """Stands in for GitHubService during an offline eval: reads and searches
    go to the pinned checkout, anything else (issues, branches, commits, PRs)
    raises, so nothing is ever written to GitHub."""

    def __init__(self, repo: Any) -> None:
        self._repo = repo

    async def get_file_contents(self, owner: str, repo: str, path: str, ref: str = "main") -> tuple[str, str]:
        path = path.lstrip("/")
        if not self._repo.file_exists(path):
            raise FileNotFoundError(f"404: {path} not in the pinned checkout")
        return self._repo.read_file(path), "local"

    async def search_code(self, owner: str, repo: str, query: str, *, strict: bool = False) -> list[dict]:
        hits = self._repo.files_containing(query.strip('"')) or set()
        return [{"path": p} for p in sorted(hits)]

    async def find_files_by_name(self, owner: str, repo: str, basename: str, ref: str = "main") -> list[str]:
        return sorted(p for p in self._repo.list_files() if p.rsplit("/", 1)[-1] == basename)

    def __getattr__(self, name: str):
        async def _blocked(*args, **kwargs):
            raise RuntimeError(f"offline eval: GitHubService.{name} is disabled")
        return _blocked


def _jsonable(value: Any) -> Any:
    """A DiagnosisResult as JSON: enums and other objects become strings."""
    return json.loads(json.dumps(value, default=str))


def _apply_diagnosis(incident: Any, diagnosis: Any) -> None:
    """The fields incident_loop.py copies from a DiagnosisResult onto the
    incident before FixGenerationAgent runs (incident_loop.py, after diagnose())."""
    incident.diagnosis = diagnosis.root_cause
    incident.confidence = diagnosis.confidence
    incident.reproduction_confirmed = diagnosis.reproduction_confirmed
    incident.diagnosis_affected_file = diagnosis.affected_file
    incident.diagnosis_affected_function = diagnosis.affected_function
    incident.diagnosis_root_cause_snippet = diagnosis.root_cause_snippet
    incident.diagnosis_additional_fix = diagnosis.additional_fix
    incident.diagnosis_additional_fix_file = diagnosis.additional_fix_file
    incident.diagnosis_additional_fix_function = diagnosis.additional_fix_function
    incident.diagnosis_additional_fix_snippet = diagnosis.additional_fix_snippet
    incident.diagnosis_additional_fix_targets = list(diagnosis.additional_fix_targets or [])
    incident.diagnosis_blast_radius = list(diagnosis.blast_radius or [])
    incident.diagnosis_contract_change = diagnosis.contract_change
    incident.diagnosis_contract_change_detail = diagnosis.contract_change_detail


def _git_diff(worktree: Path, files: dict[str, str]) -> str:
    """Write the fixed files into the worktree, take the diff, restore them."""
    for path, content in files.items():
        (worktree / path).write_text(content)
    try:
        # --no-ext-diff: a user-level diff.external (a GUI diff tool, say)
        # replaces git's patch output, and the SWE-bench harness needs a real patch.
        return subprocess.run(["git", "-C", str(worktree), "diff", "--no-ext-diff", "--no-color", "--", *files],
                              capture_output=True, text=True, check=True).stdout
    finally:
        subprocess.run(["git", "-C", str(worktree), "checkout", "--", *files], check=True)


async def run_one(instance: dict[str, Any], saved_diagnosis: dict | None = None) -> dict[str, Any]:
    from app.agents import fix_generation
    from app.agents.diagnosis import DiagnosisAgent, DiagnosisResult, _pinned_graph
    from app.models.events import ErrorEvent, EventSource, IncidentState
    from app.services import cost_meter
    from app.services.llm_gateway import llm_gateway
    from app.services.repo import LocalRepoService

    owner, repo = instance["repo"].split("/", 1)
    truth = _touched_files(instance["patch"])
    rec: dict[str, Any] = {"instance_id": instance["instance_id"], "repo": instance["repo"]}

    pinned = LocalRepoService(owner, repo, pinned_sha=instance["base_commit"])
    await pinned.ensure_fresh()
    github = LocalOnlyGitHub(pinned)
    event = ErrorEvent(source=EventSource.APPLICATION, error_type="GITHUB_ISSUE",
                       title=instance["instance_id"], description=instance["problem_statement"],
                       service=repo, metadata={})
    incident = IncidentState(error_event=event)
    try:
        # ── diagnosis, as eval_swebench_diagnosis.py replays it, or one saved
        # by an earlier run, so two fix models can be compared on identical input ──
        with cost_meter.metered() as m:
            t0 = time.monotonic()
            if saved_diagnosis is not None:
                diagnosis = DiagnosisResult(**saved_diagnosis)
            else:
                diag = DiagnosisAgent(github=github, local_repo=pinned, owner=owner, repo=repo)
                diag._llm = llm_gateway.get_llm_service_for("diagnosis")
                diagnosis = await diag.diagnose(incident)
        candidates = {f for f in (diagnosis.affected_file, diagnosis.additional_fix_file) if f}
        candidates |= {t.get("file") for t in (diagnosis.additional_fix_targets or []) if t.get("file")}
        rec["diagnosis"] = {
            "verdict": _grade(candidates, truth)[0], "affected_file": diagnosis.affected_file,
            "affected_function": diagnosis.affected_function, "confidence": diagnosis.confidence,
            "cost_usd": m.summary()["cost_usd"], "seconds": round(time.monotonic() - t0, 1),
            "meter": m.summary(), "reused": saved_diagnosis is not None,
            "full": _jsonable(dataclasses.asdict(diagnosis)),
        }
        if rec["diagnosis"]["verdict"] != "PASS":
            rec["fix"] = {"skipped": "diagnosis did not localize"}
            return rec

        # ── fix, patch only, tools pointed at the pinned checkout ──
        _apply_diagnosis(incident, diagnosis)
        agent = fix_generation.FixGenerationAgent(github=github)
        agent._owner, agent._repo, agent._local_repo, agent._rag = owner, repo, pinned, None
        # Per-agent call graph, never the module-level one: several cases run at
        # once. Shares DiagnosisAgent's cache (keyed by repo and pinned SHA).
        agent._code_graph = await _pinned_graph(owner, repo, pinned)
        agent._llm = llm_gateway.get_llm_service_for("fix")
        with cost_meter.metered() as m:
            t0 = time.monotonic()
            result, steps = await agent.fix_with_steps(incident, patch_only=True)
        patch = _git_diff(pinned.local_path, result.patched_files) if result.patched_files else ""
        rec["fix"] = {
            "target_file": result.target_file, "target_function": result.target_function,
            "description": result.fix_description, "files_changed": result.files_changed,
            "patch_lines": patch.count("\n"), "steps": steps,
            "cost_usd": m.summary()["cost_usd"], "seconds": round(time.monotonic() - t0, 1),
            "meter": m.summary(),
        }
        rec["model_patch"] = patch
        return rec
    finally:
        await pinned.remove_worktree()


async def main_async(ids: list[str], run_dir: Path, diagnoses_from: Path | None = None,
                     parallel: int = 1) -> None:
    rows = {r["instance_id"]: r for r in map(json.loads, FULL.read_text().splitlines()) if r}
    saved: dict[str, dict] = {}
    if diagnoses_from is not None:
        for line in (diagnoses_from / "results.jsonl").read_text().splitlines():
            rec = json.loads(line)
            if (rec.get("diagnosis") or {}).get("full"):
                saved[rec["instance_id"]] = rec["diagnosis"]["full"]
        missing = [i for i in ids if i not in saved]
        if missing:
            raise SystemExit(f"no saved diagnosis in {diagnoses_from} for: {missing}")
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path, preds_path = run_dir / "results.jsonl", run_dir / "predictions.jsonl"
    done = {json.loads(line)["instance_id"] for line in results_path.read_text().splitlines()} \
        if results_path.exists() else set()
    todo = [i for i in ids if i not in done]
    if len(todo) < len(ids):
        print(f"{len(ids) - len(todo)} case(s) already done, skipping")
    gate = asyncio.Semaphore(max(1, parallel))
    write_lock = asyncio.Lock()

    async def one(i: str) -> None:
        async with gate:
            try:
                rec = await run_one(rows[i], saved.get(i))
            except Exception as exc:          # one broken case shouldn't stop the run
                rec = {"instance_id": i, "error": f"{type(exc).__name__}: {exc}"}
        async with write_lock:
            with results_path.open("a") as f:
                f.write(json.dumps({k: v for k, v in rec.items() if k != "model_patch"}) + "\n")
            if rec.get("model_patch"):
                with preds_path.open("a") as f:
                    f.write(json.dumps({"instance_id": i, "model_name_or_path": MODEL_NAME,
                                        "model_patch": rec["model_patch"]}) + "\n")
        d, fx = rec.get("diagnosis") or {}, rec.get("fix") or {}
        outcome = (fx.get("skipped") or f"{fx.get('patch_lines', 0)} diff lines") if fx else rec.get("error")
        print(f"{i}: diagnosis {d.get('verdict', '-')} | fix {outcome} | "
              f"${(d.get('cost_usd') or 0) + (fx.get('cost_usd') or 0):.3f}", flush=True)

    await asyncio.gather(*(one(i) for i in todo))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--pilot", action="store_true", help="the 10 pilot cases")
    group.add_argument("--heldout", action="store_true", help="all held-out cases (xarray, sphinx)")
    group.add_argument("--all", action="store_true", help="all 500 SWE-bench Verified cases")
    group.add_argument("--cases", help="comma-separated instance ids")
    ap.add_argument("--run", required=True, help="run name: writes runs/fix/<run>/")
    ap.add_argument("--diagnoses-from", help="reuse the full diagnoses saved by this earlier run")
    ap.add_argument("--fix-model", help="LiteLLM model id for the fix task (sets LLM_MODEL_FIX)")
    ap.add_argument("--fix-max-tokens", type=int,
                    help="output limit for the fix task (sets LLM_MAX_TOKENS_FIX); reasoning models "
                         "spend it on hidden reasoning, so 8,192 cut off DeepSeek fixes")
    ap.add_argument("--parallel", type=int, default=1,
                    help="cases at once (API-bound; keep low on a laptop, e.g. 3)")
    args = ap.parse_args()
    run_dir = OUT / args.run
    run_dir.mkdir(parents=True, exist_ok=True)
    log = (run_dir / "run.log").open("a")
    logging.basicConfig(stream=log, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Every 5 minutes, dump all thread stacks to the log: a stuck API call or
    # tool loop shows up there without needing a debugger (py-spy needs root on macOS).
    faulthandler.dump_traceback_later(300, repeat=True, file=log)
    if args.fix_model:
        os.environ["LLM_MODEL_FIX"] = args.fix_model
    if args.fix_max_tokens:
        os.environ["LLM_MAX_TOKENS_FIX"] = str(args.fix_max_tokens)
    if args.pilot:
        ids = PILOT
    elif args.heldout:
        split = json.loads(SPLIT.read_text())
        ids = sorted(c for tier in split["heldout"].values() for c in tier)
    elif args.all:
        ids = [r["instance_id"] for r in map(json.loads, FULL.read_text().splitlines()) if r]
    else:
        ids = args.cases.split(",")
    asyncio.run(main_async(ids, run_dir, OUT / args.diagnoses_from if args.diagnoses_from else None,
                           args.parallel))
    return 0


if __name__ == "__main__":
    sys.exit(main())
