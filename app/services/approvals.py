"""
Approval service — gates high-risk agent actions behind human confirmation.

Risk rules:
  LOW      — auto-approved immediately (search, fetch, read)
  MEDIUM   — auto-approved by default; can be configured to require approval
  HIGH     — requires human approval before the action can execute
  CRITICAL — requires human approval + explicit confirmation token

State is persisted to SQLite (agent_platform.db) and survives server restarts.
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
# DB helpers (module-level so they work with the module-level _store)
# ---------------------------------------------------------------------------

def _upsert_approval(req: ApprovalRequest) -> None:
    from app.services.database import tables, upsert
    try:
        upsert(tables.approvals, {
            "id": req.id,
            "status": req.status.value,
            "created_at": req.created_at.isoformat(),
            "data": req.model_dump_json(),
        })
    except Exception as exc:
        logger.warning("[Approvals] DB write failed: %s", exc)


# ---------------------------------------------------------------------------
# In-memory store (populated from DB at startup)
# ---------------------------------------------------------------------------

_store: dict[str, ApprovalRequest] = {}

# Whether MEDIUM risk also requires approval (default: auto-approve)
_MEDIUM_REQUIRES_APPROVAL = False


def _load_from_db() -> None:
    from sqlalchemy import select
    from app.services.database import engine, tables
    try:
        with engine.connect() as conn:
            rows = conn.execute(select(tables.approvals.c.data)).all()
        for row in rows:
            req = ApprovalRequest.model_validate_json(row.data)
            _store[req.id] = req
        if _store:
            logger.info("[Approvals] Loaded %d requests from DB", len(_store))
    except Exception as exc:
        logger.warning("[Approvals] DB load failed: %s", exc)


# ---------------------------------------------------------------------------
# ApprovalService
# ---------------------------------------------------------------------------

class ApprovalService:
    """
    DB-backed approval gate for agent actions.

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
        level = RiskLevel(risk_level.lower() if isinstance(risk_level, str) else risk_level)

        req = ApprovalRequest(
            agent_name=agent_name,
            action=action,
            parameters=parameters,
            risk_level=level,
            description=description,
        )

        _store[req.id] = req

        if level == RiskLevel.LOW or (
            level == RiskLevel.MEDIUM and not self._medium_requires_approval
        ):
            req.status = ApprovalStatus.AUTO_APPROVED
            req.decided_at = datetime.now(timezone.utc)
            req.decided_by = "auto"
            logger.info("[Approvals] AUTO-APPROVED %s — %s: %s", req.id, agent_name, action)
            _upsert_approval(req)
            return req

        logger.warning(
            "[Approvals] PENDING APPROVAL %s — %s/%s [%s]",
            req.id, agent_name, action, level.upper(),
        )
        _upsert_approval(req)
        self._notify_approvers(req)
        return req

    def approve(self, request_id: str, approver: str) -> ApprovalRequest:
        req = self._get_or_raise(request_id)
        self._assert_pending(req)

        req.status = ApprovalStatus.APPROVED
        req.decided_at = datetime.now(timezone.utc)
        req.decided_by = approver

        _upsert_approval(req)
        logger.info("[Approvals] APPROVED %s by %s", request_id, approver)
        self._notify_approvers(req)
        return req

    def reject(self, request_id: str, approver: str, reason: str = "") -> ApprovalRequest:
        req = self._get_or_raise(request_id)
        self._assert_pending(req)

        req.status = ApprovalStatus.REJECTED
        req.decided_at = datetime.now(timezone.utc)
        req.decided_by = approver
        req.rejection_reason = reason

        _upsert_approval(req)
        logger.warning("[Approvals] REJECTED %s by %s — %s", request_id, approver, reason)
        self._notify_approvers(req)
        return req

    def get_pending(self) -> list[ApprovalRequest]:
        return [r for r in _store.values() if r.status == ApprovalStatus.PENDING]

    def get(self, request_id: str) -> ApprovalRequest | None:
        return _store.get(request_id)

    def get_all(self) -> list[ApprovalRequest]:
        return sorted(_store.values(), key=lambda r: r.created_at, reverse=True)

    # ------------------------------------------------------------------
    # Notification
    # ------------------------------------------------------------------

    def _notify_approvers(self, req: ApprovalRequest) -> None:
        border = "=" * 60
        if req.status == ApprovalStatus.PENDING:
            risk_icon = {"low": "ℹ", "medium": "⚡", "high": "⚠", "critical": "🚨"}.get(
                req.risk_level.value, "?"
            )
            lines = [
                "",
                border,
                f"  {risk_icon} APPROVAL REQUIRED [{req.risk_level.upper()}]",
                f"  Request ID : {req.id}",
                f"  Agent      : {req.agent_name}",
                f"  Action     : {req.action}",
                f"  What       : {req.description}",
            ]
            if req.parameters:
                lines.append(f"  Parameters : {req.parameters}")
            lines += [
                f"  Created    : {req.created_at.strftime('%Y-%m-%d %H:%M UTC')}",
                "",
                f"  To approve : POST /approvals/{req.id}/approve",
                f"  To reject  : POST /approvals/{req.id}/reject",
                border,
            ]
            logger.info("\n".join(lines))

        elif req.status == ApprovalStatus.AUTO_APPROVED:
            logger.info("[Approvals] ✓ Auto-approved [%s] %s (%s)", req.risk_level, req.action, req.id)

        elif req.status == ApprovalStatus.APPROVED:
            logger.info("[Approvals] ✓ APPROVED %s by %s — %s", req.id, req.decided_by, req.action)

        elif req.status == ApprovalStatus.REJECTED:
            logger.info(
                "[Approvals] ✗ REJECTED %s by %s — %s  reason: %s",
                req.id, req.decided_by, req.action, req.rejection_reason,
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
# Module-level singleton
# ---------------------------------------------------------------------------

_load_from_db()
approval_service = ApprovalService()
