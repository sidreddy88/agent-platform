"""
Evals API.

POST /evals/run        run the golden dataset through one model
POST /evals/ab         run A/B comparison (two models)
GET  /evals/dataset    list the golden eval cases (no LLM calls)
"""
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, HTTPException

from app.services.eval_runner import eval_runner

router = APIRouter(prefix="/evals", tags=["evals"])


@router.post("/run")
async def run_evals(
    model: Optional[str] = Body(default=None, embed=True),
) -> Dict[str, Any]:
    """
    Run the full golden dataset through the TriageAgent with the given model.

    Omit `model` to use the default Haiku model.
    """
    try:
        result = await eval_runner.run_dataset(model=model)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return {
        "run_id":    result.run_id,
        "model":     result.model,
        "timestamp": result.timestamp,
        "summary": {
            "total":     result.total,
            "passed":    result.passed,
            "failed":    result.failed,
            "pass_rate": result.pass_rate,
            "p50_ms":    result.p50_ms,
            "p95_ms":    result.p95_ms,
        },
        "cases": [
            {
                "case_id":          cr.case_id,
                "description":      cr.description,
                "passed":           cr.passed,
                "decision_correct": cr.decision_correct,
                "severity_correct": cr.severity_correct,
                "triage_decision":  cr.triage_decision,
                "triage_severity":  cr.triage_severity,
                "expected_decision": cr.expected_decision,
                "expected_severity": cr.expected_severity,
                "duration_ms":      cr.duration_ms,
                "error":            cr.error,
            }
            for cr in result.cases
        ],
    }


@router.post("/ab")
async def run_ab(
    model_a: Optional[str] = Body(default=None, embed=True),
    model_b: Optional[str] = Body(default=None, embed=True),
) -> Dict[str, Any]:
    """
    Run the golden dataset through two models and return a side-by-side comparison.

    Defaults to Haiku (A) vs Sonnet (B).
    """
    try:
        ab = await eval_runner.run_ab(model_a=model_a, model_b=model_b)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    def _run_summary(r) -> Dict[str, Any]:
        return {
            "run_id":    r.run_id,
            "model":     r.model,
            "total":     r.total,
            "passed":    r.passed,
            "pass_rate": r.pass_rate,
            "p50_ms":    r.p50_ms,
            "p95_ms":    r.p95_ms,
        }

    return {
        "run_id":           ab.run_id,
        "timestamp":        ab.timestamp,
        "model_a":          _run_summary(ab.model_a),
        "model_b":          _run_summary(ab.model_b),
        "pass_rate_delta":  ab.pass_rate_delta,
        "latency_delta_ms": ab.latency_delta_ms,
        "winner":           ab.winner,
        "verdict":          ab.verdict,
    }


@router.get("/dataset")
async def get_dataset() -> List[Dict[str, Any]]:
    """List the golden eval cases — no LLM calls."""
    try:
        cases = eval_runner.load_dataset()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return [
        {
            "id":          c.id,
            "description": c.description,
            "tags":        c.tags,
            "input":       c.input,
            "expected":    c.expected,
        }
        for c in cases
    ]
