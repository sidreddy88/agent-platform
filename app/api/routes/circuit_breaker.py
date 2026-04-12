"""
Circuit breaker API.

GET  /circuit-breakers             list all breakers and their states
POST /circuit-breakers/{name}/reset  manually force a breaker to CLOSED
"""
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from app.services.circuit_breaker import circuit_breaker_registry

router = APIRouter(prefix="/circuit-breakers", tags=["circuit-breakers"])


@router.get("", response_model=List[Dict[str, Any]])
async def list_breakers():
    """Return state info for every registered circuit breaker."""
    return circuit_breaker_registry.all_states()


@router.post("/{name}/reset")
async def reset_breaker(name: str) -> Dict[str, Any]:
    """
    Manually reset a circuit breaker to CLOSED.

    Use after the underlying service has recovered and you want to stop
    rejecting calls immediately (rather than waiting for the timeout).
    """
    ok = circuit_breaker_registry.reset(name)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Circuit breaker '{name}' not found")
    return {"name": name, "state": "closed", "message": "Reset to CLOSED by operator"}
