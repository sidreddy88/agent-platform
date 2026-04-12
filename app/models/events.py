from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
import uuid


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
    REVIEWING = "reviewing"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    REJECTED = "rejected"   # Human rejected the AI fix
    NOISE = "noise"         # Triage determined it's not real
    DUPLICATE = "duplicate"  # Existing PR already covers this


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

    # Fix
    fix_attempted: Optional[str] = None
    fix_description: Optional[str] = None   # full FixResult.fix_description (for RLHF logging)
    pr_url: Optional[str] = None
    pr_number: Optional[int] = None
    review_posted: bool = False
    approval_id: Optional[str] = None

    # Human decision
    human_decision: Optional[str] = None  # "approved" | "rejected"
    human_decision_reason: Optional[str] = None
    outcome: Optional[str] = None

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
            return (self.resolved_at - self.detected_at).total_seconds()
        return None

    @property
    def age_seconds(self) -> float:
        return (datetime.utcnow() - self.detected_at).total_seconds()
