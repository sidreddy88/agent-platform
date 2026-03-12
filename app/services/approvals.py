"""
Approval service — gates high-risk agent actions behind human confirmation.

Risk rules:
  LOW      — auto-approved immediately (search, fetch, read)
  MEDIUM   — auto-approved by default; can be configured to require approval
  HIGH     — requires human approval before the action can execute
  CRITICAL — requires human approval + explicit confirmation token

Usage:
    svc = ApprovalService()

    req = await svc.request_approval(
        agent_name="IncidentResponseAgent",
        action="restart_ecs_service",
        parameters={"cluster": "prod", "service": "api"},
        risk_level="high",
        description="Restart the ECS api service to recover from task crash-loop.",
    )

    if req.status == "approved":
        # execute the action
        ...
    else:
        print(f"Pending approval: {req.id}")
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    AUTO_APPROVED = "auto_approved"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class ApprovalRequest(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    agent_name: str
    action: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk_level: RiskLevel
    description: str                    # plain-English "what will happen"
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    decided_at: datetime | None = None
    decided_by: str | None = None       # approver username or "auto"
    rejection_reason: str | None = None


class ApprovalDecision(BaseModel):
    request_id: str
    approved: bool
    decided_by: str
    reason: str | None = None


# ---------------------------------------------------------------------------
# ApprovalService
# ---------------------------------------------------------------------------

# Module-level store — replace with a database for production
_store: dict[str, ApprovalRequest] = {}

# Whether MEDIUM risk also requires approval (default: auto-approve)
_MEDIUM_REQUIRES_APPROVAL = False


class ApprovalService:
    """
    In-memory approval gate for agent actions.

    LOW and MEDIUM actions are auto-approved by default.
    HIGH and CRITICAL actions are queued as PENDING until a human decides.
    """

    def __init__(self, medium_requires_approval: bool = _MEDIUM_REQUIRES_APPROVAL) -> None:
        self._medium_requires_approval = medium_requires_approval

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def request_approval(
        self,
        agent_name: str,
        action: str,
        parameters: dict[str, Any],
        risk_level: str | RiskLevel,
        description: str,
    ) -> ApprovalRequest:
        """
        Create an approval request for an action.

        LOW/MEDIUM → auto-approved immediately (unless medium_requires_approval=True).
        HIGH/CRITICAL → queued as PENDING; notifies approvers.

        Returns the ApprovalRequest. Check `.status` to know whether to proceed.
        """
        level = RiskLevel(risk_level.lower() if isinstance(risk_level, str) else risk_level)

        req = ApprovalRequest(
            agent_name=agent_name,
            action=action,
            parameters=parameters,
            risk_level=level,
            description=description,
        )

        _store[req.id] = req

        # Auto-approve safe actions
        if level == RiskLevel.LOW or (
            level == RiskLevel.MEDIUM and not self._medium_requires_approval
        ):
            req.status = ApprovalStatus.AUTO_APPROVED
            req.decided_at = datetime.now(timezone.utc)
            req.decided_by = "auto"
            logger.info("[Approvals] AUTO-APPROVED %s — %s: %s", req.id, agent_name, action)
            return req

        # Queue for human review
        logger.warning(
            "[Approvals] PENDING APPROVAL %s — %s/%s [%s]",
            req.id, agent_name, action, level.upper(),
        )
        self._notify_approvers(req)
        return req

    def approve(self, request_id: str, approver: str) -> ApprovalRequest:
        """Approve a pending request."""
        req = self._get_or_raise(request_id)
        self._assert_pending(req)

        req.status = ApprovalStatus.APPROVED
        req.decided_at = datetime.now(timezone.utc)
        req.decided_by = approver

        logger.info("[Approvals] APPROVED %s by %s", request_id, approver)
        self._notify_approvers(req)
        return req

    def reject(self, request_id: str, approver: str, reason: str = "") -> ApprovalRequest:
        """Reject a pending request."""
        req = self._get_or_raise(request_id)
        self._assert_pending(req)

        req.status = ApprovalStatus.REJECTED
        req.decided_at = datetime.now(timezone.utc)
        req.decided_by = approver
        req.rejection_reason = reason

        logger.warning("[Approvals] REJECTED %s by %s — %s", request_id, approver, reason)
        self._notify_approvers(req)
        return req

    def get_pending(self) -> list[ApprovalRequest]:
        """Return all requests still waiting for a decision."""
        return [r for r in _store.values() if r.status == ApprovalStatus.PENDING]

    def get(self, request_id: str) -> ApprovalRequest | None:
        """Return a request by ID, or None if not found."""
        return _store.get(request_id)

    def get_all(self) -> list[ApprovalRequest]:
        """Return all requests (any status), newest first."""
        return sorted(_store.values(), key=lambda r: r.created_at, reverse=True)

    # ------------------------------------------------------------------
    # Notification (console for now — extend for Slack/email later)
    # ------------------------------------------------------------------

    def _notify_approvers(self, req: ApprovalRequest) -> None:
        """
        Notify approvers of a new or decided request.
        Currently prints to console. Replace with Slack/email/PagerDuty later.
        """
        border = "=" * 60
        if req.status == ApprovalStatus.PENDING:
            risk_icon = {"low": "ℹ", "medium": "⚡", "high": "⚠", "critical": "🚨"}.get(
                req.risk_level.value, "?"
            )
            print(f"\n{border}")
            print(f"  {risk_icon} APPROVAL REQUIRED [{req.risk_level.upper()}]")
            print(f"  Request ID : {req.id}")
            print(f"  Agent      : {req.agent_name}")
            print(f"  Action     : {req.action}")
            print(f"  What       : {req.description}")
            if req.parameters:
                print(f"  Parameters : {req.parameters}")
            print(f"  Created    : {req.created_at.strftime('%Y-%m-%d %H:%M UTC')}")
            print(f"\n  To approve : POST /approvals/{req.id}/approve")
            print(f"  To reject  : POST /approvals/{req.id}/reject")
            print(f"{border}\n")

        elif req.status == ApprovalStatus.AUTO_APPROVED:
            print(f"[Approvals] ✓ Auto-approved [{req.risk_level}] {req.action} ({req.id})")

        elif req.status == ApprovalStatus.APPROVED:
            print(f"\n[Approvals] ✓ APPROVED {req.id} by {req.decided_by} — {req.action}\n")

        elif req.status == ApprovalStatus.REJECTED:
            print(
                f"\n[Approvals] ✗ REJECTED {req.id} by {req.decided_by} "
                f"— {req.action}  reason: {req.rejection_reason}\n"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_or_raise(self, request_id: str) -> ApprovalRequest:
        req = _store.get(request_id)
        if req is None:
            raise KeyError(f"Approval request '{request_id}' not found")
        return req

    @staticmethod
    def _assert_pending(req: ApprovalRequest) -> None:
        if req.status != ApprovalStatus.PENDING:
            raise ValueError(
                f"Request '{req.id}' is already {req.status} — cannot change decision"
            )


# ---------------------------------------------------------------------------
# Module-level singleton (shared across routes and agents)
# ---------------------------------------------------------------------------

approval_service = ApprovalService()
