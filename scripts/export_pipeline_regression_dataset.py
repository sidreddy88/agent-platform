"""
Export ground truth for every merged-fix incident into a local regression
dataset, for scripts/eval_pipeline_regression.py to replay against.

Real motivation: after any change to DiagnosisAgent's prompt or logic, there
was no way to check "did this still find the right file in the incidents it
used to get right" short of waiting for a new real incident. This captures
the ground truth (what DiagnosisAgent actually found, on the incidents that
went on to merge a real fix) so it can be replayed later.

Ground truth lives in the live Postgres incidents table only -- it's runtime
state, not reproducible from a fresh clone, and (per README's own caveat)
incidents can be cleared/deduped, which would lose it permanently. This
script's whole job is capturing it into a stable local file before that
happens.

Uses app.services.database's engine/tables directly (same pattern as
scripts/measure_diagnosis_grounding.py), so it works unmodified against
local dev sqlite AND live production Postgres. In production this means
running it via `aws ecs execute-command` inside the container -- the DB
isn't publicly reachable (see infra/agent_platform's security group
comments).

Output: app/evals/pipeline_regression.jsonl (gitignored -- real incident
data, same handling as app/evals/golden_dataset.jsonl).

Usage:
    python scripts/export_pipeline_regression_dataset.py
    python scripts/export_pipeline_regression_dataset.py --out /tmp/regression.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "app" / "evals" / "pipeline_regression.jsonl"


def _load_merged_incidents() -> list[dict[str, Any]]:
    from sqlalchemy import select

    from app.services.database import engine, tables

    with engine.connect() as conn:
        rows = conn.execute(select(tables.incidents.c.data)).all()

    incidents: list[dict[str, Any]] = []
    for row in rows:
        try:
            blob = json.loads(row.data)
        except (json.JSONDecodeError, TypeError):
            continue
        if blob.get("outcome") != "fix_merged":
            continue
        if not blob.get("diagnosis_affected_file"):
            # No file identified means there's nothing meaningful to check
            # a future diagnosis against -- skip rather than record a
            # ground truth of "found nothing" as if it were a target.
            continue
        incidents.append(blob)
    return incidents


def _to_regression_case(blob: dict[str, Any]) -> dict[str, Any]:
    """Extract just what replay + scoring need -- not the full IncidentState."""
    event = blob.get("error_event") or {}
    return {
        "incident_id": blob.get("id"),
        "event": {
            "source": event.get("source", "cloudwatch"),
            "error_type": event.get("error_type"),
            "title": event.get("title", ""),
            "description": event.get("description", ""),
            "service": event.get("service", ""),
            "metadata": event.get("metadata", {}),
        },
        "ground_truth": {
            "affected_file": blob.get("diagnosis_affected_file"),
            "affected_function": blob.get("diagnosis_affected_function"),
            "confidence": blob.get("confidence"),
            "pr_url": blob.get("pr_url"),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help="Output JSONL path")
    args = parser.parse_args()

    blobs = _load_merged_incidents()
    cases = [_to_regression_case(b) for b in blobs]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for case in cases:
            f.write(json.dumps(case) + "\n")

    print(f"Exported {len(cases)} regression case(s) to {out_path}")
    if not cases:
        print("No merged-fix incidents with an identified affected_file found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
