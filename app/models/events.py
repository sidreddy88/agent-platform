from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


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
    # Routing tag for the dashboard's Errors / Non-errors sub-tabs.
    # Thrown application errors stay "error"; deprecation warnings and
    # network-timeout classes (which don't usually indicate a code bug)
    # land as "non_error".
    category: str = "error"
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
    FIX_FAILED = "fix_failed"  # FixGenerationAgent could not produce a PR — escalated to human


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
    diagnosis_affected_file: Optional[str] = None      # primary fix file identified by DiagnosisAgent
    diagnosis_affected_function: Optional[str] = None  # primary fix function identified by DiagnosisAgent
    diagnosis_additional_fix: Optional[str] = None         # secondary fix description
    diagnosis_additional_fix_file: Optional[str] = None    # secondary fix file
    diagnosis_additional_fix_function: Optional[str] = None  # secondary fix function
    diagnosis_additional_fix_snippet: Optional[str] = None  # grounded snippet backing
    # ^ additional_fix_file -- was added to DiagnosisResult in PR #178 but never
    # persisted here, so it never reached FixGenerationAgent's secondary-fix anchor
    # search for the single-file case (blast_radius entries got this from day one).
    diagnosis_additional_fix_targets: List[Dict[str, Any]] = Field(default_factory=list)
    # ^ each entry: {"file": str, "function": str|None, "snippet": str|None} -- the
    # multi-file counterpart to diagnosis_additional_fix_file/_function, for the
    # copy-pasted-per-brand/tenant duplication case where MULTIPLE sibling files need
    # the identical fix (diagnosis_additional_fix_file only ever carries one). Each
    # entry is grounded the same way as diagnosis_blast_radius entries -- see
    # DiagnosisAgent._enforce_grounding.
    # Pre-fix-reasoning fields (populated by DiagnosisAgent, consumed by FixGenerationAgent
    # as Tier 2 constraints in the fix prompt).
    diagnosis_blast_radius: List[Dict[str, Any]] = Field(default_factory=list)
    # ^ each entry: {"file": str, "function": str, "snippet": str}
    diagnosis_contract_change: Optional[str] = None  # "none" | "signature" | "return_type" | "side_effect"
    diagnosis_contract_change_detail: Optional[str] = None

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
    merge_decision: Optional[str] = None           # "merge_now" | "refix_first" — set by MergeDecisionAgent
    merge_decision_reasoning: Optional[str] = None
    clarity_summary: Optional[str] = None          # ErrorClarityAgent: why root cause is unclear
    clarity_pr_url: Optional[str] = None           # observability PR created by ErrorClarityAgent
    clarity_pr_number: Optional[int] = None

    @field_validator("approval_id", mode="before")
    @classmethod
    def _coerce_approval_id(cls, v: Any) -> Optional[str]:
        if isinstance(v, (list, dict)):
            return None
        return v

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
