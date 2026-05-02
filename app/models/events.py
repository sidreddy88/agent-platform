from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _as_naive(dt: datetime) -> datetime:
    """Strip timezone info so naive and aware datetimes can be compared."""
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


class EventSource(str, Enum):
    CLOUDWATCH = "cloudwatch"
    DIGITAL_OCEAN = "digital_ocean"
    CLOUDFLARE = "cloudflare"
    APPLICATION = "application"


class Severity(str, Enum):
    P0 = "P0"  # Critical — service down, immediate action
    P1 = "P1"  # High — degraded, urgent
    P2 = "P2"  # Medium — warning
    P3 = "P3"  # Low — informational


class ErrorEvent(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    source: EventSource
    severity: Optional[Severity] = None   # null at detection; set by TriageAgent
    error_type: Optional[str] = None      # e.g. "S3_NO_SUCH_KEY"
    task_id: Optional[str] = None         # ECS task ID when applicable
    title: str
    description: str
    service: str
    resource_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    detected_at: datetime = Field(default_factory=datetime.utcnow)


class IncidentStatus(str, Enum):
    OPEN = "open"
    TRIAGING = "triaging"
    DIAGNOSING = "diagnosing"
    FIXING = "fixing"
    AWAITING_FIX_APPROVAL = "awaiting_fix_approval"  # diff generated, waiting for human to approve before commit
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    REJECTED = "rejected"   # Human rejected the AI fix
    NOISE = "noise"         # Triage determined it's not real
    DUPLICATE = "duplicate"  # Existing PR already covers this
    VERIFICATION_FAILED = "verification_failed"  # DoD gate blocked REVIEWING transition
    AWAITING_REFIX_APPROVAL = "awaiting_refix_approval"  # Code review requested changes — awaiting human go/no-go


class IncidentState(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    error_event: ErrorEvent
    status: IncidentStatus = IncidentStatus.OPEN

    # Triage
    triage_decision: Optional[str] = None  # "real" | "noise" | "duplicate"
    triage_reasoning: Optional[str] = None
    blast_radius: Optional[str] = None
    occurrences_24h: Optional[int] = None

    # Diagnosis
    diagnosis: Optional[str] = None
    confidence: Optional[float] = None  # 0.0 - 1.0
    reproduction_confirmed: Optional[bool] = None
    diagnosis_affected_file: Optional[str] = None      # producer file identified by DiagnosisAgent
    diagnosis_affected_function: Optional[str] = None  # producer function identified by DiagnosisAgent

    # Fix
    fix_attempted: Optional[str] = None
    fix_description: Optional[str] = None   # full FixResult.fix_description (for RLHF logging)
    pending_fix_file: Optional[str] = None   # file path of pending diff awaiting approval
    pending_fix_old: Optional[str] = None    # verbatim old code awaiting approval
    pending_fix_new: Optional[str] = None    # proposed new code awaiting approval
    pending_fix_branch: Optional[str] = None  # branch + issue already created for pending fix
    pending_fix_issue_url: Optional[str] = None
    pending_fix_issue_number: Optional[int] = None
    pending_fix_function: Optional[str] = None
    pending_fix_critique: Optional[str] = None  # self-critique result
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    pr_branch: Optional[str] = None
    pr_files_changed: List[str] = Field(default_factory=list)
    pr_test_added: bool = False
    review_posted: bool = False
    approval_id: Optional[str] = None

    # Human decision
    human_decision: Optional[str] = None  # "approved" | "rejected"
    human_decision_reason: Optional[str] = None
    human_notes: Optional[str] = None  # human feedback injected into fix prompt on restart
    outcome: Optional[str] = None

    # Post-resolution metadata
    archived: bool = False
    wrong_fix: bool = False
    wrong_fix_notes: Optional[str] = None

    # Harness / pipeline metadata
    monitor_id: Optional[str] = None        # resource_id of the monitor/alarm that triggered this incident; None for manually triggered
    issue_url: Optional[str] = None         # GitHub issue URL created by FixGenerationAgent
    dod_failed_checks: Optional[Dict[str, str]] = None  # {check_name: evidence} populated when VERIFICATION_FAILED

    # Tracing
    trace_id: Optional[str] = None

    # Timing — all in UTC
    detected_at: datetime = Field(default_factory=datetime.utcnow)
    triage_completed_at: Optional[datetime] = None
    diagnosis_completed_at: Optional[datetime] = None
    pr_created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    @property
    def mttr_seconds(self) -> Optional[float]:
        if self.resolved_at:
            return (_as_naive(self.resolved_at) - _as_naive(self.detected_at)).total_seconds()
        return None

    @property
    def age_seconds(self) -> float:
        return (datetime.utcnow() - _as_naive(self.detected_at)).total_seconds()
