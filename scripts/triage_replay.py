"""
One-shot replay: fetch today's most recent NoSuchKey event from CloudWatch,
build an ErrorEvent, and run it through the full pipeline:
  TriageAgent → DiagnosisAgent → FixGenerationAgent → CodeReviewAgent

Usage:
    python scripts/triage_replay.py [--no-fix] [--synthetic]

  --no-fix     Stop after diagnosis (skip fix generation and PR creation)
  --synthetic  Skip CloudWatch lookup and triage/diagnosis — jump straight
               to fix generation using a hardcoded incident (useful when no
               live events are available in the current lookback window)

Output: full pipeline result printed to stdout + sent to Slack.
"""
import asyncio
import json
import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.triage import TriageAgent
from app.agents.diagnosis import DiagnosisAgent, CONFIDENCE_THRESHOLD
from app.agents.fix_generation import FixGenerationAgent
from app.agents.code_review import CodeReviewAgent
from app.core.config import settings
from app.models.events import ErrorEvent, EventSource, IncidentState, IncidentStatus, Severity
from app.services.aws import AWSService, AWSError

LOG_GROUP = settings.ecs_log_groups or "/ecs/target-app-task"
PATTERN = "NoSuchKey"
ERROR_TYPE = "S3_NO_SUCH_KEY"
SERVICE = "target-app"
LOOKBACK_HOURS = 24  # search window


def build_event(match: dict) -> ErrorEvent:
    stream = match["stream"]
    # ECS log stream format: prefix/container/task-id
    stream_parts = stream.rsplit("/", 1)
    task_id = stream_parts[-1] if len(stream_parts) > 1 else stream

    return ErrorEvent(
        source=EventSource.CLOUDWATCH,
        severity=None,
        error_type=ERROR_TYPE,
        task_id=task_id,
        title=f"{ERROR_TYPE}: {SERVICE}",
        description=match["message"][:300],
        service=SERVICE,
        resource_id=LOG_GROUP,
        metadata={
            "log_group": LOG_GROUP,
            "pattern": PATTERN,
            "match_count": 1,
            "latest_timestamp": match["timestamp"],
            "task_id": task_id,
        },
    )


NO_FIX = "--no-fix" in sys.argv
SYNTHETIC = "--synthetic" in sys.argv


def _synthetic_incident() -> IncidentState:
    """Return a hardcoded IncidentState for the known NoSuchKey incident."""
    event = ErrorEvent(
        source=EventSource.CLOUDWATCH,
        severity=Severity.P2,
        error_type=ERROR_TYPE,
        task_id="synthetic-task-id",
        title=f"{ERROR_TYPE}: {SERVICE}",
        description=(
            "NoSuchKey: The specified key does not exist. "
            "at moveAndRemoveFileFromS3 (routes/services/image.js)"
        ),
        service=SERVICE,
        resource_id=LOG_GROUP,
        metadata={
            "log_group": LOG_GROUP,
            "pattern": PATTERN,
            "match_count": 47,
            "evidence": [
                f"47 occurrences of NoSuchKey in {LOG_GROUP} in last 24h",
                "Error originates in moveAndRemoveFileFromS3 at routes/services/image.js",
                "S3 key missing at copy step — race condition between upload and move",
            ],
        },
    )
    return IncidentState(
        error_event=event,
        status=IncidentStatus.FIXING,
        triage_decision="real",
        triage_reasoning="47 occurrences/24h, affects image processing for all users",
        blast_radius="All image upload/move operations",
        occurrences_24h=47,
        diagnosis=(
            "Race condition in moveAndRemoveFileFromS3: S3 copy fails with NoSuchKey "
            "when the source key is deleted or never created before the move runs. "
            "The function lacks error handling for this case, causing unhandled exceptions."
        ),
        confidence=0.92,
        reproduction_confirmed=True,
    )


async def main() -> None:
    if SYNTHETIC:
        print("--synthetic mode: skipping CloudWatch lookup, using hardcoded incident")
        incident = _synthetic_incident()
        print(f"\nSynthetic incident id: {incident.id}")
        print(f"  error_type : {incident.error_event.error_type}")
        print(f"  diagnosis  : {incident.diagnosis[:80]}...")
        print(f"  confidence : {incident.confidence:.0%}")
        await _run_fix_and_review(incident)
        return

    aws = AWSService()

    print(f"Searching {LOG_GROUP} for '{PATTERN}' in the last {LOOKBACK_HOURS}h...")
    try:
        matches = aws.search_log_events(
            log_group=LOG_GROUP,
            filter_pattern=PATTERN,
            minutes=LOOKBACK_HOURS * 60,
            limit=50,
        )
    except AWSError as exc:
        print(f"CloudWatch fetch failed: {exc}")
        sys.exit(1)

    if not matches:
        print(f"No '{PATTERN}' events found in the last {LOOKBACK_HOURS}h. Nothing to triage.")
        sys.exit(0)

    latest = matches[0]
    print(f"\nFound {len(matches)} occurrence(s). Using most recent:")
    print(f"  timestamp : {latest['timestamp']}")
    print(f"  stream    : {latest['stream']}")
    print(f"  message   : {latest['message'][:120]}")

    event = build_event(latest)
    print(f"\nErrorEvent id: {event.id}")
    print(f"  error_type : {event.error_type}")
    print(f"  task_id    : {event.task_id}")

    # ── Step 1: Triage ──────────────────────────────────────────────
    print("\nStep 1 — TriageAgent (Haiku)...")
    triage_agent = TriageAgent(aws=aws)
    triage = await triage_agent.triage(event)

    print("\n" + "=" * 60)
    print("TRIAGE RESULT")
    print("=" * 60)
    print(json.dumps({
        "decision": triage.decision,
        "severity": triage.severity,
        "blast_radius": triage.blast_radius,
        "occurrences_24h": triage.occurrences_24h,
        "duplicate_pr": triage.duplicate_pr,
        "reasoning": triage.reasoning,
    }, indent=2))
    print("=" * 60)

    if triage.decision != "real":
        print(f"\nStopping — decision is '{triage.decision}', no diagnosis needed.")
        return

    # ── Step 2: Diagnosis ────────────────────────────────────────────
    print(f"\nStep 2 — DiagnosisAgent (Sonnet)...")
    try:
        event.severity = Severity[triage.severity]
    except KeyError:
        event.severity = Severity.P2

    incident = IncidentState(
        error_event=event,
        status=IncidentStatus.DIAGNOSING,
        triage_decision=triage.decision,
        triage_reasoning=triage.reasoning,
        blast_radius=triage.blast_radius,
        occurrences_24h=triage.occurrences_24h,
    )

    diagnosis_agent = DiagnosisAgent(aws=aws)
    diagnosis = await diagnosis_agent.diagnose(incident)

    print("\n" + "=" * 60)
    print("DIAGNOSIS RESULT")
    print("=" * 60)
    print(json.dumps({
        "root_cause": diagnosis.root_cause,
        "confidence": diagnosis.confidence,
        "evidence": diagnosis.evidence,
        "fix_approach": diagnosis.fix_approach,
        "affected_function": diagnosis.affected_function,
        "affected_file": diagnosis.affected_file,
        "reproduction_confirmed": diagnosis.reproduction_confirmed,
        "escalate": diagnosis.escalate,
        "next_step": "AWAITING_APPROVAL (human review)" if diagnosis.escalate
                     else f"FIXING (confidence {diagnosis.confidence:.0%} ≥ {CONFIDENCE_THRESHOLD:.0%})",
    }, indent=2))
    print("=" * 60)

    if diagnosis.escalate:
        print("\nStopping — confidence too low for automated fix. Human review required.")
        return

    if NO_FIX:
        print("\n--no-fix flag set — skipping fix generation.")
        return

    incident.status = IncidentStatus.FIXING
    incident.diagnosis = diagnosis.root_cause
    incident.confidence = diagnosis.confidence
    incident.reproduction_confirmed = diagnosis.reproduction_confirmed

    await _run_fix_and_review(incident)


async def _run_fix_and_review(incident: IncidentState) -> None:
    """Steps 3+4: FixGenerationAgent → CodeReviewAgent."""

    # ── Step 3: Fix Generation ───────────────────────────────────────
    print(f"\nStep 3 — FixGenerationAgent (Sonnet) — creating Issue + PR...")

    fix_agent = FixGenerationAgent()
    fix, agent_steps = await fix_agent.fix_with_steps(incident)

    print("\n" + "=" * 60)
    print("FIX AGENT — STEPS")
    print("=" * 60)
    for step in agent_steps:
        print(f"  {step}")
    print("=" * 60)

    print("\n" + "=" * 60)
    print("FIX RESULT")
    print("=" * 60)
    print(json.dumps({
        "issue_url": fix.issue_url,
        "pr_url": fix.pr_url,
        "pr_number": fix.pr_number,
        "branch": fix.branch,
        "files_changed": fix.files_changed,
        "test_added": fix.test_added,
        "fix_description": fix.fix_description[:200],
    }, indent=2))
    print("=" * 60)

    if not fix.pr_url and not fix.pr_number:
        print("\nFix generation did not produce a PR — see ReAct steps above for errors.")
        return

    # ── Step 4: Code Review ──────────────────────────────────────────
    print(f"\nStep 4 — CodeReviewAgent — reviewing PR #{fix.pr_number} ({fix.pr_url})...")
    owner, repo = settings.fix_target_repo.split("/", 1)
    review_agent = CodeReviewAgent()
    review_result = await review_agent.run(
        f'{{"owner": "{owner}", "repo": "{repo}", '
        f'"pr_number": {fix.pr_number}, "post_to_github": true}}'
    )

    print("\n" + "=" * 60)
    print("CODE REVIEW")
    print("=" * 60)
    print(review_result.answer)
    print("=" * 60)

    posted = "✓ Review posted to GitHub." in review_result.answer
    print(f"\nPipeline complete.")
    print(f"  Issue  : {fix.issue_url}")
    print(f"  PR     : {fix.pr_url}")
    print(f"  Review : {'posted to GitHub' if posted else 'NOT posted — see warning above'}")


if __name__ == "__main__":
    asyncio.run(main())
