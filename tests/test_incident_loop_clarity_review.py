"""
Regression tests for CodeReviewAgent being wired into ErrorClarityAgent's PR path.

Real gap: CodeReviewAgent was only ever invoked after fix.pr_url (two call sites in
incident_loop.py, both inside the FixGenerationAgent path) — ErrorClarityAgent's PRs
(observability-only additions, opened when diagnosis confidence is too low to safely
generate a fix) went straight to a human with zero automated review.

_run_review's actual contract only ever touches .pr_number/.pr_url (see
HandoffValidator.validate_fix_for_review), both of which ClarityResult already has —
so it works with either result type without modification. review_kind is threaded
through so CodeReviewAgent applies clarity-appropriate criteria (no root-cause/
symptom-fix checks — there's no bug being fixed) instead of fix criteria.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.error_clarity import ClarityResult
from app.agents.fix_generation import FixResult
from app.models.events import ErrorEvent, EventSource, IncidentState, Severity
from app.services.incident_loop import IncidentLoop


def _make_loop(review_answer: str = "LOOKS CORRECT") -> tuple[IncidentLoop, MagicMock]:
    loop = IncidentLoop.__new__(IncidentLoop)
    review_agent = MagicMock()
    review_agent.run = AsyncMock(return_value=MagicMock(answer=review_answer))
    loop._review_agent = review_agent
    return loop, review_agent


def _make_incident() -> IncidentState:
    event = ErrorEvent(
        source=EventSource.CLOUDWATCH,
        title="Reserved schema pathname warning",
        description="errors is a reserved schema pathname",
        service="api",
        severity=Severity.P3,
    )
    return IncidentState(id="test-incident-1", error_event=event)


@pytest.mark.asyncio
async def test_run_review_accepts_clarity_result():
    """The type-widening claim: _run_review works with a ClarityResult exactly
    like it already does with a FixResult — no special-casing needed, since both
    only need .pr_number/.pr_url."""
    loop, review_agent = _make_loop()
    incident = _make_incident()
    clarity = ClarityResult(
        summary="Added a log line to ValidationLog.js",
        pr_url="https://github.com/owner/repo/pull/99",
        pr_number=99,
    )

    result = await loop._run_review(incident, clarity, review_kind="clarity")

    assert result == "LOOKS CORRECT"
    review_agent.run.assert_called_once()


@pytest.mark.asyncio
async def test_run_review_passes_review_kind_clarity_in_payload():
    loop, review_agent = _make_loop()
    incident = _make_incident()
    clarity = ClarityResult(
        summary="x", pr_url="https://github.com/owner/repo/pull/99", pr_number=99,
    )

    await loop._run_review(incident, clarity, review_kind="clarity")

    payload = review_agent.run.call_args[0][0]
    assert '"review_kind": "clarity"' in payload
    assert '"pr_number": 99' in payload


@pytest.mark.asyncio
async def test_run_review_defaults_to_fix_review_kind():
    """Existing FixGenerationAgent call sites don't pass review_kind explicitly —
    must default to "fix" so their review criteria are unchanged."""
    loop, review_agent = _make_loop()
    incident = _make_incident()
    fix = FixResult(
        issue_url=None, branch="fix/x", fix_description="x",
        pr_url="https://github.com/owner/repo/pull/50", pr_number=50,
    )

    await loop._run_review(incident, fix)

    payload = review_agent.run.call_args[0][0]
    assert '"review_kind": "fix"' in payload


@pytest.mark.asyncio
async def test_run_review_skips_when_no_pr_number():
    """A ClarityResult that only flagged a pattern (no PR opened) must not
    attempt a review — nothing to review."""
    loop, review_agent = _make_loop()
    incident = _make_incident()
    clarity = ClarityResult(summary="No specific code found", pr_number=None)

    result = await loop._run_review(incident, clarity, review_kind="clarity")

    assert result is None
    review_agent.run.assert_not_called()
