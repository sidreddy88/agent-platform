"""One-off: turn the 4 real, distinct severe incidents found by the
CloudWatch survey into real training cases -- historical occurrence
count mocked in (a live call would see 0 for a 2024/2025 incident),
real TriageAgent judgment taken as-is, not filtered against a predicted
target (these are real incidents, not synthetic-category generation)."""
import asyncio
import json
import sys
sys.path.insert(0, ".")

from scripts.generate_triage_synthetic_dataset import _ControllableAWSStub, _ControllableStoreStub

REAL_INCIDENTS = [
    {
        "error_type": "HEAP_OUT_OF_MEMORY",
        "title": "HEAP_OUT_OF_MEMORY in allinterviews-api",
        "description": "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out "
                        "of memory. Recurring since 2024-12-23, ongoing through 2026-08-29 "
                        "(57 distinct days affected).",
        "service": "allinterviews-api",
        "occurrence_count": 16,  # real peak single-day count
        "note": "Real, ongoing ~2yr memory-exhaustion problem, unresolved as of last survey pass.",
    },
    {
        "error_type": "MODULE_NOT_FOUND",
        "title": "MODULE_NOT_FOUND in allinterviews-api",
        "description": "Error: Cannot find module '../../../models/CityNationalTwoInterviewUser' "
                        "-- broken deploy, 2024-12-06 to 2024-12-07.",
        "service": "allinterviews-api",
        "occurrence_count": 15,  # real peak single-day count
        "note": "Real 2-day broken-deploy episode.",
    },
    {
        "error_type": "ECONNREFUSED",
        "title": "ECONNREFUSED in allinterviews-api",
        "description": "AxiosError: connect ECONNREFUSED -- downstream service unreachable, "
                        "single-day outage on 2026-07-28.",
        "service": "allinterviews-api",
        "occurrence_count": 8,  # real count that day
        "note": "Real single-day outage.",
    },
    {
        "error_type": "SIGKILL",
        "title": "SIGKILL in allinterviews-api",
        "description": "node completed with null:SIGKILL -- container forcibly terminated "
                        "(likely OOM-killer), 2026-08-11.",
        "service": "allinterviews-api",
        "occurrence_count": 1,  # real count
        "note": "Real single occurrence, likely correlated with the heap-exhaustion problem.",
    },
]


async def main():
    from app.agents.triage import TriageAgent
    from app.models.events import ErrorEvent, EventSource

    cases = []
    for inc in REAL_INCIDENTS:
        event = ErrorEvent(
            source=EventSource.APPLICATION, error_type=inc["error_type"],
            title=inc["title"], description=inc["description"], service=inc["service"],
            metadata={"log_group": "/ecs/TaskAllInterviews", "pattern": inc["error_type"]},
        )
        aws_stub = _ControllableAWSStub(occurrence_count=inc["occurrence_count"])
        store_stub = _ControllableStoreStub(duplicate_pr_url=None)
        agent = TriageAgent(aws=aws_stub, store=store_stub)
        result = await agent.triage(event)

        case = {
            "id": f"real_cw_{inc['error_type'].lower()}",
            "input": {
                "error_type": inc["error_type"], "title": inc["title"],
                "description": inc["description"], "service": inc["service"],
                "source": "cloudwatch", "log_group": "/ecs/TaskAllInterviews",
                "occurrence_count": inc["occurrence_count"], "has_existing_pr": False,
            },
            "output": {
                "decision": result.decision, "severity": result.severity,
                "reasoning": result.reasoning,
            },
            "metadata": {
                "tags": ["real-cloudwatch"],
                "source_note": inc["note"],
            },
        }
        print(json.dumps(case, indent=2))
        cases.append(case)

    with open("app/evals/triage_synthetic_dataset.jsonl", "a") as f:
        for case in cases:
            f.write(json.dumps(case) + "\n")
    print(f"\nAppended {len(cases)} real cases.")


asyncio.run(main())
