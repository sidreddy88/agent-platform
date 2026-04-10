"""
One-shot replay: fetch today's most recent NoSuchKey event from CloudWatch,
build an ErrorEvent, and run it through the TriageAgent.

Usage:
    python scripts/triage_replay.py

Output: triage decision printed to stdout + sent to Slack.
"""
import asyncio
import json
import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agents.triage import TriageAgent
from app.models.events import ErrorEvent, EventSource
from app.services.aws import AWSService, AWSError

LOG_GROUP = "/ecs/TaskTargetApp"
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

    print("\nRunning TriageAgent (Haiku)...")
    agent = TriageAgent(aws=aws)
    result = await agent.triage(event)

    print("\n" + "=" * 60)
    print("TRIAGE RESULT")
    print("=" * 60)
    print(json.dumps({
        "decision": result.decision,
        "severity": result.severity,
        "blast_radius": result.blast_radius,
        "occurrences_24h": result.occurrences_24h,
        "duplicate_pr": result.duplicate_pr,
        "reasoning": result.reasoning,
    }, indent=2))
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
