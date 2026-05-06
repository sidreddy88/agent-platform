"""
Agent failure dataset — lets humans annotate agent runs that produced wrong outputs.
Accumulates training examples for prompt improvement.

Failures are append-only — there is no delete endpoint by design. Once a failure
is recorded it stays in the dataset permanently so the training corpus can only
grow.

Endpoints:
  GET  /failures              — list all failures (newest first)
  POST /failures              — create a failure annotation
  GET  /failures/export       — download as JSONL for training
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.services.database import get_db
from app.services.incident_store import incident_store

router = APIRouter(prefix="/failures", tags=["failures"])

VALID_AGENTS = {
    "triage", "diagnosis", "fix_generation", "code_review",
    "merge_decision", "error_clarity", "other",
}

VALID_CATEGORIES = {
    "wrong_diagnosis",
    "wrong_file",
    "wrong_fix",
    "hallucination",
    "missed_root_cause",
    "code_not_found",
    "symptom_fix",
    "wrong_agent_decision",
    "other",
}


class CreateFailureBody(BaseModel):
    incident_id: str
    run_id: Optional[str] = None
    agent_name: str
    failure_category: str
    failure_reason: str
    expected_behavior: Optional[str] = None


def _actual_behavior_for(incident_id: str, agent_name: str) -> str:
    """Pull the relevant agent output from incident state."""
    inc = incident_store.get(incident_id)
    if not inc:
        return ""
    if agent_name == "triage":
        parts = [inc.triage_decision or ""]
        if inc.triage_reasoning:
            parts.append(inc.triage_reasoning)
        return " | ".join(filter(None, parts))
    if agent_name == "diagnosis":
        parts = []
        if inc.diagnosis:
            parts.append(inc.diagnosis)
        if inc.confidence is not None:
            parts.append(f"confidence={inc.confidence:.0%}")
        if inc.diagnosis_affected_file:
            parts.append(f"file={inc.diagnosis_affected_file}")
        if inc.diagnosis_affected_function:
            parts.append(f"fn={inc.diagnosis_affected_function}")
        return " | ".join(parts)
    if agent_name == "fix_generation":
        return inc.fix_description or inc.fix_attempted or ""
    if agent_name == "code_review":
        return inc.pending_fix_critique or ""
    if agent_name == "merge_decision":
        parts = [inc.merge_decision or ""]
        if inc.merge_decision_reasoning:
            parts.append(inc.merge_decision_reasoning)
        return " | ".join(filter(None, parts))
    if agent_name == "error_clarity":
        return inc.clarity_summary or ""
    return ""


def _error_description_for(incident_id: str) -> str:
    inc = incident_store.get(incident_id)
    if not inc:
        return ""
    ev = inc.error_event
    return f"{ev.error_type or ev.title}: {ev.description[:300]}"


@router.get("")
def list_failures(agent: Optional[str] = None, limit: int = 200):
    conn = get_db()
    try:
        if agent:
            rows = conn.execute(
                "SELECT * FROM agent_failures WHERE agent_name = ? ORDER BY created_at DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_failures ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("", status_code=201)
def create_failure(body: CreateFailureBody):
    if body.agent_name not in VALID_AGENTS:
        raise HTTPException(400, f"agent_name must be one of {sorted(VALID_AGENTS)}")
    if body.failure_category not in VALID_CATEGORIES:
        raise HTTPException(400, f"failure_category must be one of {sorted(VALID_CATEGORIES)}")

    failure_id = uuid.uuid4().hex
    actual = _actual_behavior_for(body.incident_id, body.agent_name)
    error_desc = _error_description_for(body.incident_id)
    created_at = datetime.utcnow().isoformat()

    conn = get_db()
    try:
        conn.execute(
            """
            INSERT INTO agent_failures
                (id, incident_id, run_id, agent_name, failure_category,
                 failure_reason, expected_behavior, actual_behavior,
                 error_description, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                failure_id,
                body.incident_id,
                body.run_id,
                body.agent_name,
                body.failure_category,
                body.failure_reason,
                body.expected_behavior,
                actual,
                error_desc,
                created_at,
            ),
        )
        conn.commit()
        return {"id": failure_id, "created_at": created_at}
    finally:
        conn.close()


@router.get("/export", response_class=PlainTextResponse)
def export_failures():
    """Export all failures as JSONL — one JSON object per line."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM agent_failures ORDER BY created_at ASC"
        ).fetchall()
        lines = [json.dumps(dict(r)) for r in rows]
        return "\n".join(lines)
    finally:
        conn.close()
