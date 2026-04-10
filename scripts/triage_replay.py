"""
One-shot replay: fetch today's most recent NoSuchKey event from CloudWatch,
build an ErrorEvent, and run it through the full pipeline:
  TriageAgent → DiagnosisAgent

Usage:
    python scripts/triage_replay.py

Output: triage + diagnosis printed to stdout + sent to Slack.
"""
import asyncio
import json
import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.triage import TriageAgent
from app.agents.diagnosis import DiagnosisAgent, CONFIDENCE_THRESHOLD
from app.models.events import ErrorEvent, EventSource, IncidentState, IncidentStatus, Severity
from app.services.aws import AWSService, AWSError

LOG_GROUP = "/ecs/TaskAllInterviews"
PATTERN = "NoSuchKey"
ERROR_TYPE = "S3_NO_SUCH_KEY"
SERVICE = "allinterviews"
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


async def main() -> None:
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


if __name__ == "__main__":
    asyncio.run(main())
