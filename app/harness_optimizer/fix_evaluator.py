"""
Evaluate a fix-agent harness directory: run FixGenerationAgent with it on
SWE-bench cases and grade the patches with the official harness (resolved =
pass), the fix-agent counterpart of evaluator.ReplayEvaluator.

- Only the fix step varies: each case reuses a saved diagnosis (run 1's, by
  default), so a candidate is compared with the incumbent on identical input.
- Cases must have a localized diagnosis: run_one skips the fix when the
  diagnosis missed, and that check uses the true fix files, so a non-localized
  case would leak "diagnosis was wrong" into the score. build-time check below.
- Patches are graded per trial in one batch (Modal by default). Trajectories
  carry the agent's own steps and an outcome of "resolved" / "patch failed the
  tests" / "no patch" only: never the gold patch, the true files or test names
  (the lesson of the #272 answer leak).
- Escalation = no patch (the agent gave up or declined), the fix-step analogue
  of diagnosis's "no accepted submission".
"""
from __future__ import annotations

import asyncio
import glob
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from app.harness_optimizer.acceptance import CaseResult, EvalResult
from app.harness_optimizer.evaluator import EvalOutcome, ProviderFailure, UnpricedModel

ROOT = Path(__file__).resolve().parent.parent.parent
RUN1 = ROOT / "runs" / "fix" / "all500-run1" / "results.jsonl"
FULL = ROOT / "app" / "evals" / "swebench_verified_full.jsonl"
GRADE_SCRIPT = ROOT / "scripts" / "swebench_eval_x86.py"
SWEBENCH_PY = os.environ.get("SWEBENCH_PY", str(Path.home() / ".venvs" / "swebench" / "bin" / "python"))

RunCase = Callable[[dict, dict, str], Awaitable[dict]]
Grade = Callable[[list[dict], str], dict[str, bool]]

OUTCOME_TEXT = {"resolved": "resolved", "failed": "patch failed the tests", "no_patch": "no patch"}


def load_saved_diagnoses(path: Path = RUN1) -> dict[str, dict]:
    """instance_id -> saved full diagnosis, for cases whose diagnosis localized."""
    out = {}
    for line in path.read_text().splitlines():
        rec = json.loads(line)
        d = rec.get("diagnosis") or {}
        if d.get("verdict") == "PASS" and d.get("full"):
            out[rec["instance_id"]] = d["full"]
    return out


def _default_run_case(fix_model: str | None = None) -> RunCase:
    async def run(instance: dict, saved_diagnosis: dict, harness_dir: str) -> dict:
        from scripts.eval_swebench_fix import run_one
        return await run_one(instance, saved_diagnosis=saved_diagnosis, harness_dir=harness_dir,
                             fix_model=fix_model)
    return run


def modal_grade(predictions: list[dict], run_id: str, workdir: Path | None = None) -> dict[str, bool]:
    """Grade predictions with the official SWE-bench harness on Modal; instance_id -> resolved."""
    if not predictions:
        return {}
    work = Path(workdir or ROOT / "runs" / "fix" / "_optimizer_grading") / run_id
    work.mkdir(parents=True, exist_ok=True)
    (work / "predictions.jsonl").write_text("".join(json.dumps(p) + "\n" for p in predictions))
    proc = subprocess.run(
        [SWEBENCH_PY, str(GRADE_SCRIPT), "--dataset_name", "princeton-nlp/SWE-bench_Verified",
         "--predictions_path", "predictions.jsonl", "--run_id", run_id, "--max_workers", "8",
         "--modal", "true"], cwd=work, capture_output=True, text=True, timeout=3 * 3600)
    (work / "grade.log").write_text(proc.stdout[-200_000:] + proc.stderr[-50_000:])
    reports = {}
    for f in glob.glob(str(work / "logs" / "run_evaluation" / run_id / "*" / "*" / "report.json")):
        reports.update(json.loads(Path(f).read_text()))
    missing = [p["instance_id"] for p in predictions if p["instance_id"] not in reports]
    if missing:
        raise ProviderFailure(f"grading produced no report for {len(missing)} of {len(predictions)} "
                              f"patches (e.g. {missing[:3]}); see {work / 'grade.log'}")
    return {iid: bool(r.get("resolved")) for iid, r in reports.items()}


class FixReplayEvaluator:
    # The loop hands batched evaluators chunks of cases; each trial of a chunk is
    # graded in one Modal run (per-case grading took ~1 min per case-trial).
    batch_size = 25

    def __init__(self, saved_diagnoses: dict[str, dict] | None = None,
                 instances_path: Path = FULL, parallel: int = 5,
                 run_case: RunCase | None = None, grade: Grade | None = None,
                 fix_model: str | None = None) -> None:
        self.on_trial: Callable[[], None] | None = None
        self._diagnoses = saved_diagnoses if saved_diagnoses is not None else load_saved_diagnoses()
        self._instances = {}
        if instances_path.exists():
            for line in instances_path.read_text().splitlines():
                if line.strip():
                    inst = json.loads(line)
                    self._instances[inst["instance_id"]] = inst
        self._parallel = parallel
        # fix_model overrides the routed fix model for this evaluator only: the
        # optimizer's second-model check runs alongside the primary evaluator.
        self._run_case = run_case or _default_run_case(fix_model)
        self._grade = grade or modal_grade

    def check_cases(self, case_ids: list[str]) -> None:
        bad = [c for c in case_ids if c not in self._diagnoses]
        if bad:
            raise ValueError(f"{len(bad)} cases have no localized saved diagnosis (e.g. {bad[:3]}); "
                             "the fix evaluator only scores localized cases")

    async def evaluate(self, harness_dir: Path, case_ids: list[str], trials: int) -> EvalOutcome:
        from app.services import cost_meter

        self.check_cases(case_ids)
        per_case = {cid: CaseResult(passes=0, trials=0) for cid in case_ids}
        trajectories: list[dict] = []
        total = 0.0
        sem = asyncio.Semaphore(self._parallel)

        for t in range(trials):
            recs: dict[str, dict] = {}

            async def one(cid: str) -> None:
                async with sem:
                    with cost_meter.metered() as meter:
                        rec = await self._run_case(self._instances[cid], self._diagnoses[cid], str(harness_dir))
                    rec["_meter"] = meter.summary()
                    recs[cid] = rec
                    # Progress per finished fix run, not only after a trial's batch is graded:
                    # a 25-case chunk can take longer than the loop's 20-minute stall watchdog.
                    if self.on_trial is not None:
                        self.on_trial()

            await asyncio.gather(*(one(c) for c in case_ids))

            preds = []
            for cid, rec in recs.items():
                summary = rec["_meter"]
                if summary.get("unpriced_models"):
                    raise UnpricedModel(f"{cid} trial {t + 1}: no price for {summary['unpriced_models']}")
                fix = rec.get("fix") or {}
                if "error" in fix:
                    raise ProviderFailure(f"{cid} trial {t + 1}: {str(fix['error'])[:300]}", cost_usd=total)
                if fix.get("provider_failure"):
                    # A credit/auth/outage error inside the fix loop would otherwise score as
                    # "no patch". Pause instead: finished cases are cached, resume redoes the rest.
                    raise ProviderFailure(f"{cid} trial {t + 1}: provider failure in the fix loop "
                                          f"({fix['provider_failure']})", cost_usd=total)
                if rec.get("model_patch"):
                    preds.append({"instance_id": cid, "model_name_or_path": "fix-optimizer",
                                  "model_patch": rec["model_patch"]})

            run_id = f"fixopt-{time.strftime('%Y%m%d-%H%M%S')}-t{t + 1}-{uuid.uuid4().hex[:6]}"
            resolved = await asyncio.to_thread(self._grade, preds, run_id)

            for cid, rec in recs.items():
                cost = rec["_meter"].get("cost_usd") or 0.0
                total += cost
                if not rec.get("model_patch"):
                    outcome = "no_patch"
                else:
                    outcome = "resolved" if resolved.get(cid) else "failed"
                cr = per_case[cid]
                cr.trials += 1
                cr.passes += outcome == "resolved"
                cr.escalations += outcome == "no_patch"
                cr.cost_usd += cost
                cr.verdicts.append("PASS" if outcome == "resolved" else "FAIL")
                fix = rec.get("fix") or {}
                trajectories.append({
                    "instance_id": cid, "repo": self._instances[cid]["repo"], "trial": t + 1,
                    "verdict": "PASS" if outcome == "resolved" else "FAIL",
                    "detail": OUTCOME_TEXT[outcome],
                    "steps": fix.get("trajectory_steps") or [],
                    "self_feedback": fix.get("self_feedback"),
                    # No per-call records (the fix loop doesn't capture prompt segments), so
                    # cost-by-source analysis skips these; the turn count is kept separately.
                    "llm_calls": [],
                    "turns": int((rec["_meter"] or {}).get("calls") or 0),
                    "cost": rec["_meter"],
                })
                if self.on_trial is not None:
                    self.on_trial()
        trajectories.sort(key=lambda r: (r["instance_id"], r["trial"]))   # finish order varies
        return EvalOutcome(EvalResult(per_case), total, trajectories)
