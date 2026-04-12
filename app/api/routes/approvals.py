"""
Approval endpoints — human-facing API to review and decide on agent action requests.

GET  /approvals/pending          list all pending approval requests
GET  /approvals                  list all requests (any status)
GET  /approvals/{id}             get a single request
POST /approvals/{id}/approve     approve a pending request
POST /approvals/{id}/reject      reject a pending request
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.models.events import IncidentStatus
from app.services.approvals import ApprovalRequest, approval_service
from app.services.incident_store import incident_store

router = APIRouter(prefix="/approvals", tags=["approvals"])


# ---------------------------------------------------------------------------
# Request/response bodies
# ---------------------------------------------------------------------------

class ApproveBody(BaseModel):
    approver: str


class RejectBody(BaseModel):
    approver: str
    reason: str = ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/pending", response_model=list[ApprovalRequest])
async def get_pending():
    """List all approval requests waiting for a human decision."""
    return approval_service.get_pending()


@router.get("", response_model=list[ApprovalRequest])
async def get_all():
    """List all approval requests (any status), newest first."""
    return approval_service.get_all()


@router.get("/{request_id}", response_model=ApprovalRequest)
async def get_one(request_id: str):
    """Get a single approval request by ID."""
    req = approval_service.get(request_id)
    if req is None:
        raise HTTPException(status_code=404, detail=f"Approval request '{request_id}' not found")
    return req


@router.post("/{request_id}/approve", response_model=ApprovalRequest)
async def approve(request_id: str, body: ApproveBody):
    """Approve a pending approval request and resolve the linked incident."""
    try:
        req = approval_service.approve(request_id, body.approver)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    incident_id = req.parameters.get("incident_id")
    if incident_id:
        incident = incident_store.get(incident_id)
        if incident:
            incident.human_decision = "approved"
            incident.outcome = "fix_merged"
            incident.status = IncidentStatus.RESOLVED
            incident.resolved_at = datetime.utcnow()
            incident_store.update(incident)

    return req


@router.post("/{request_id}/reject", response_model=ApprovalRequest)
async def reject(request_id: str, body: RejectBody):
    """Reject a pending approval request and mark the linked incident as rejected."""
    try:
        req = approval_service.reject(request_id, body.approver, body.reason)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    incident_id = req.parameters.get("incident_id")
    if incident_id:
        incident = incident_store.get(incident_id)
        if incident:
            incident.human_decision = "rejected"
            incident.human_decision_reason = body.reason
            incident.outcome = "fix_rejected"
            incident.status = IncidentStatus.REJECTED
            incident.resolved_at = datetime.utcnow()
            incident_store.update(incident)

    return req
