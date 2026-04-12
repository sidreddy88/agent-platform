"""
Failure injection API.

POST /injection/trigger   inject a failure scenario into the live pipeline
GET  /injection/scenarios list available scenarios with descriptions
"""
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.failure_injection import FailureScenario, failure_injector

router = APIRouter(prefix="/injection", tags=["injection"])

_SCENARIO_DOCS: dict[str, str] = {
    FailureScenario.FALSE_POSITIVE: (
        "A single HEALTH_CHECK_TIMEOUT that TriageAgent should classify as noise. "
        "Validates the noise-detection path."
    ),
    FailureScenario.DUPLICATE_ALERT: (
        "Two identical DB_CONNECTION_POOL_EXHAUSTED events fired 100ms apart. "
        "The second should be deduplicated by the orchestrator."
    ),
    FailureScenario.CASCADING_FAILURE: (
        "A 4-event chain: postgres overload → redis miss rate → API latency → queue depth. "
        "Tests priority-lane routing and concurrent processing."
    ),
}


class TriggerBody(BaseModel):
    scenario: FailureScenario
    service: str | None = None   # optional service name override


@router.get("/scenarios", response_model=List[Dict[str, Any]])
async def list_scenarios():
    """List all available failure injection scenarios."""
    return [
        {"scenario": s.value, "description": _SCENARIO_DOCS.get(s, "")}
        for s in FailureScenario
    ]


@router.post("/trigger")
async def trigger_injection(body: TriggerBody) -> Dict[str, Any]:
    """
    Inject a failure scenario into the live pipeline.

    Returns a summary of what was enqueued so you can track the events
    through the dashboard.
    """
    try:
        result = await failure_injector.inject(body.scenario, service=body.service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return result
