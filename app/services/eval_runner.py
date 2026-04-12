"""
Eval runner — measures triage-agent accuracy against a golden dataset.

Scoring
-------
Each eval case provides:
  expected.triage_decision   "real" | "noise" | "duplicate"
  expected.triage_severity   list of acceptable values, e.g. ["P1","P2"]

A case passes when BOTH fields match.  Severity is allowed to be any value
in the list (models may reasonably disagree by one level).

A/B testing
-----------
run_ab() runs the full dataset through two model configurations and returns
a side-by-side diff of pass rate and p50 latency, plus a verdict.

AWS / store isolation
---------------------
In eval mode the TriageAgent's tool calls are intercepted by lightweight
stubs so evals never require live AWS credentials:
  - occurrence count  → "5 occurrences in the last 24 hours"
  - duplicate check   → "NO_DUPLICATE: No existing PR found"

Usage
-----
    from app.services.eval_runner import eval_runner
    result = await eval_runner.run_dataset(model="claude-haiku-4-5-20251001")
    ab     = await eval_runner.run_ab()   # Sonnet vs Haiku
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from app.agents.triage import TriageAgent
from app.models.events import ErrorEvent, EventSource
from app.services.llm import LLMService

logger = logging.getLogger(__name__)

_DATASET_PATH = Path(__file__).parent.parent / "evals" / "golden_dataset.jsonl"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class EvalCase:
    id: str
    description: str
    input: dict[str, Any]
    expected: dict[str, Any]
    tags: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case_id: str
    description: str
    model: str
    triage_decision: Optional[str]
    triage_severity: Optional[str]
    expected_decision: str
    expected_severity: list[str]
    decision_correct: bool
    severity_correct: bool
    passed: bool
    duration_ms: int
    error: Optional[str] = None


@dataclass
class EvalRunResult:
    run_id: str
    model: str
    timestamp: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    p50_ms: Optional[float]
    p95_ms: Optional[float]
    cases: list[CaseResult] = field(default_factory=list)


@dataclass
class ABResult:
    run_id: str
    timestamp: str
    model_a: EvalRunResult
    model_b: EvalRunResult
    pass_rate_delta: float          # model_b.pass_rate − model_a.pass_rate
    latency_delta_ms: Optional[float]  # model_b.p50 − model_a.p50
    winner: str                     # "model_a" | "model_b" | "tie"
    verdict: str


# ---------------------------------------------------------------------------
# EvalRunner
# ---------------------------------------------------------------------------

class EvalRunner:

    # ------------------------------------------------------------------
    # Dataset loading
    # ------------------------------------------------------------------

    def load_dataset(self, path: Path = _DATASET_PATH) -> list[EvalCase]:
        """Load and parse the golden JSONL dataset."""
        if not path.exists():
            raise FileNotFoundError(f"Golden dataset not found: {path}")
        cases: list[EvalCase] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                cases.append(EvalCase(
                    id=raw["id"],
                    description=raw["description"],
                    input=raw["input"],
                    expected=raw["expected"],
                    tags=raw.get("tags", []),
                ))
        return cases

    # ------------------------------------------------------------------
    # Single-case evaluation
    # ------------------------------------------------------------------

    async def run_case(self, case: EvalCase, model: str) -> CaseResult:
        """
        Run triage on one eval case with the specified model.

        AWS tool calls are stubbed so no live credentials are needed.
        """
        inp = case.input
        try:
            source = EventSource(inp.get("source", "application"))
        except ValueError:
            source = EventSource.APPLICATION

        event = ErrorEvent(
            source=source,
            error_type=inp["error_type"],
            title=inp["title"],
            description=inp["description"],
            service=inp.get("service", "unknown"),
        )

        agent = TriageAgent(
            llm=LLMService(model=model),
            aws=_EvalAWSStub(),
            store=_EvalStoreStub(),
        )

        start = time.perf_counter()
        triage_decision = None
        triage_severity = None
        error_msg = None

        try:
            result = await agent.triage(event)
            triage_decision = result.decision
            triage_severity = result.severity
        except Exception as exc:
            logger.warning("[EvalRunner] case %s failed: %s", case.id, exc)
            error_msg = str(exc)

        duration_ms = int((time.perf_counter() - start) * 1000)

        expected_decision = case.expected["triage_decision"]
        expected_severity = case.expected["triage_severity"]

        decision_correct = triage_decision == expected_decision
        severity_correct = triage_severity in expected_severity if triage_severity else False
        passed = decision_correct and severity_correct

        return CaseResult(
            case_id=case.id,
            description=case.description,
            model=model,
            triage_decision=triage_decision,
            triage_severity=triage_severity,
            expected_decision=expected_decision,
            expected_severity=expected_severity,
            decision_correct=decision_correct,
            severity_correct=severity_correct,
            passed=passed,
            duration_ms=duration_ms,
            error=error_msg,
        )

    # ------------------------------------------------------------------
    # Full dataset run
    # ------------------------------------------------------------------

    async def run_dataset(
        self,
        model: Optional[str] = None,
        dataset: Optional[list[EvalCase]] = None,
    ) -> EvalRunResult:
        """
        Run all golden cases through triage with the given model.

        model=None defaults to Haiku (same as production TriageAgent).
        """
        from app.services.llm import HAIKU_MODEL  # noqa: PLC0415 — avoids circular at module init
        model = model or HAIKU_MODEL

        if dataset is None:
            dataset = self.load_dataset()

        case_results: list[CaseResult] = []
        for case in dataset:
            cr = await self.run_case(case, model)
            case_results.append(cr)
            logger.info(
                "[EvalRunner] %s  %s  decision=%s(%s) severity=%s(%s)",
                "✓" if cr.passed else "✗",
                cr.case_id,
                cr.triage_decision, cr.expected_decision,
                cr.triage_severity, cr.expected_severity,
            )

        passed = sum(1 for r in case_results if r.passed)
        total  = len(case_results)
        durations = sorted(r.duration_ms for r in case_results)

        return EvalRunResult(
            run_id=f"run_{uuid.uuid4().hex[:8]}",
            model=model,
            timestamp=datetime.now(timezone.utc).isoformat(),
            total=total,
            passed=passed,
            failed=total - passed,
            pass_rate=passed / total if total else 0.0,
            p50_ms=_percentile(durations, 50) if durations else None,
            p95_ms=_percentile(durations, 95) if durations else None,
            cases=case_results,
        )

    # ------------------------------------------------------------------
    # A/B comparison
    # ------------------------------------------------------------------

    async def run_ab(
        self,
        model_a: Optional[str] = None,
        model_b: Optional[str] = None,
        dataset: Optional[list[EvalCase]] = None,
    ) -> ABResult:
        """
        Run the golden dataset through two models and compare results.

        Defaults to Haiku (A) vs Sonnet (B).
        """
        from app.services.llm import HAIKU_MODEL, MODEL as SONNET_MODEL  # noqa: PLC0415
        model_a = model_a or HAIKU_MODEL
        model_b = model_b or SONNET_MODEL

        if dataset is None:
            dataset = self.load_dataset()

        logger.info("[EvalRunner] A/B: %s vs %s (%d cases)", model_a, model_b, len(dataset))

        result_a = await self.run_dataset(model=model_a, dataset=dataset)
        result_b = await self.run_dataset(model=model_b, dataset=dataset)

        delta_rate = result_b.pass_rate - result_a.pass_rate
        delta_ms: Optional[float] = None
        if result_a.p50_ms is not None and result_b.p50_ms is not None:
            delta_ms = result_b.p50_ms - result_a.p50_ms

        if abs(delta_rate) < 0.05:
            winner = "tie"
        elif delta_rate > 0:
            winner = "model_b"
        else:
            winner = "model_a"

        verdict = _build_verdict(model_a, model_b, result_a, result_b, winner, delta_rate, delta_ms)

        return ABResult(
            run_id=f"ab_{uuid.uuid4().hex[:8]}",
            timestamp=datetime.now(timezone.utc).isoformat(),
            model_a=result_a,
            model_b=result_b,
            pass_rate_delta=delta_rate,
            latency_delta_ms=delta_ms,
            winner=winner,
            verdict=verdict,
        )


# ---------------------------------------------------------------------------
# Eval stubs — isolate evals from live AWS / incident store
# ---------------------------------------------------------------------------

class _EvalAWSStub:
    """Returns deterministic canned responses for every TriageAgent AWS tool."""

    def get_log_occurrences(self, *args, **kwargs) -> int:
        return 5

    def get_metric_statistics(self, *args, **kwargs):
        return None

    def get_ecs_status(self, *args, **kwargs):
        return None

    def get_cloudwatch_alarms(self, *args, **kwargs):
        return []

    # Catch-all so any attribute access returns a no-op callable
    def __getattr__(self, name):
        return lambda *a, **k: None


class _EvalStoreStub:
    """Returns no existing PRs so evals never see duplicate signals."""

    def get_pr_for_resource(self, *args, **kwargs):
        return None

    def set_pr_for_resource(self, *args, **kwargs):
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _percentile(sorted_values: list[float], pct: float) -> float:
    import math
    if not sorted_values:
        raise ValueError("empty")
    idx = max(0, math.ceil(len(sorted_values) * pct / 100) - 1)
    return sorted_values[min(idx, len(sorted_values) - 1)]


def _build_verdict(
    model_a: str, model_b: str,
    res_a: EvalRunResult, res_b: EvalRunResult,
    winner: str, delta_rate: float, delta_ms: Optional[float],
) -> str:
    a_name = model_a.split("-")[1] if "-" in model_a else model_a   # e.g. "haiku"
    b_name = model_b.split("-")[1] if "-" in model_b else model_b   # e.g. "sonnet"

    rate_line = (
        f"{a_name} {res_a.pass_rate:.0%} vs {b_name} {res_b.pass_rate:.0%} "
        f"(Δ {delta_rate:+.0%})"
    )
    lat_line = ""
    if delta_ms is not None:
        lat_line = (
            f"  |  p50 latency: {res_a.p50_ms:.0f}ms vs {res_b.p50_ms:.0f}ms "
            f"(Δ {delta_ms:+.0f}ms)"
        )

    if winner == "tie":
        conclusion = f"No meaningful difference (< 5pp). {a_name} is faster/cheaper if latency favours it."
    elif winner == "model_b":
        conclusion = f"{b_name} wins on accuracy ({delta_rate:+.0%})."
    else:
        conclusion = f"{a_name} wins on accuracy ({-delta_rate:+.0%})."

    return f"{rate_line}{lat_line}  |  {conclusion}"


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

eval_runner = EvalRunner()
