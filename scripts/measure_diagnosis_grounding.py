"""
Measure how often DiagnosisAgent's submit_diagnosis gate actually catches and
corrects a fabricated/ungrounded claim before a diagnosis finalizes.

Real motivation: PR #211 replaced a post-hoc, one-shot-retry grounding audit
with an inline gate the model sees and must satisfy before finalizing (see
DiagnosisAgent._validate_diagnosis_submission). This turns "the gate probably
helps" into a measured number: what fraction of diagnoses needed at least one
correction, same style as the existing 92% triage accuracy / <8% false-positive
numbers already quoted in the README.

Uses app.services.database's engine/tables directly (NOT a hardcoded sqlite
path like scripts/measure_mttr.py) so this same script works unmodified
against local dev sqlite AND the live production Postgres DB -- it reads
whatever DATABASE_URL the running app is actually configured with. In
production this means running it via `aws ecs execute-command` inside the
container (the DB is not publicly reachable — see infra/agent_platform's
security group comments), not from a local machine.

Important caveat: diagnosis_grounding_rejections is a NEW field (added
alongside this script). Every incident diagnosed BEFORE this shipped has it
defaulting to 0 regardless of what actually happened at the time -- there was
no instrumentation to record a real value. Use --since with this PR's deploy
date to see only genuinely-tracked data; anything before that date is not
"zero rejections observed", it's "not measured".

Usage:
    python scripts/measure_diagnosis_grounding.py
    python scripts/measure_diagnosis_grounding.py --since 2026-08-21
    python scripts/measure_diagnosis_grounding.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_diagnosed_incidents(since: str | None) -> list[dict[str, Any]]:
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
        if not blob.get("diagnosis_completed_at"):
            continue  # never reached diagnosis (noise/duplicate/triage-only)
        if since and (blob.get("diagnosis_completed_at") or "") < since:
            continue
        incidents.append(blob)
    return incidents


def _summarize(incidents: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(incidents)
    if total == 0:
        return {"total": 0}

    rejection_counts = [int(inc.get("diagnosis_grounding_rejections") or 0) for inc in incidents]
    with_rejections = sum(1 for c in rejection_counts if c > 0)
    distribution = Counter(rejection_counts)

    # Fail-closed diagnoses (confidence 0.0, the "never got a grounded
    # submission through" fallback from diagnose()) are the extreme case --
    # the gate held the line for the whole run, not just one field.
    fail_closed = sum(
        1 for inc in incidents
        if inc.get("confidence") == 0.0
        and (inc.get("diagnosis") or "").startswith("Diagnosis could not be grounded")
    )

    return {
        "total": total,
        "with_at_least_one_rejection": with_rejections,
        "rejection_rate_pct": round(100 * with_rejections / total, 1),
        "fail_closed_count": fail_closed,
        "fail_closed_rate_pct": round(100 * fail_closed / total, 1),
        "rejection_count_distribution": dict(sorted(distribution.items())),
        "avg_rejections_per_diagnosis": round(sum(rejection_counts) / total, 2),
    }


def _print_report(summary: dict[str, Any], since: str | None) -> None:
    total = summary.get("total", 0)
    if total == 0:
        scope = f" since {since}" if since else ""
        print(f"No diagnosed incidents found{scope}.")
        return

    print(f"# DiagnosisAgent grounding-rejection rate (N={total}{f', since {since}' if since else ''})")
    print()
    print(f"- **Diagnoses needing at least one correction:** "
          f"{summary['with_at_least_one_rejection']}/{total} "
          f"({summary['rejection_rate_pct']}%)")
    print(f"- **Diagnoses that never got a grounded submission through "
          f"(fail-closed):** {summary['fail_closed_count']}/{total} "
          f"({summary['fail_closed_rate_pct']}%)")
    print(f"- **Avg rejections per diagnosis:** {summary['avg_rejections_per_diagnosis']}")
    print()
    print("## Rejection count distribution")
    print()
    print("| Rejections before acceptance | Count |")
    print("|---|---|")
    for count, n in summary["rejection_count_distribution"].items():
        label = f"{count}" if count < 3 else f"{count}+"
        print(f"| {label} | {n} |")
    print()
    if not since:
        print(
            "> ⚠ No --since filter applied. Incidents diagnosed before "
            "diagnosis_grounding_rejections existed all default to 0 regardless "
            "of what actually happened -- that's \"not measured\", not \"zero "
            "rejections\". Re-run with --since <this PR's deploy date> for a "
            "number that only reflects genuinely-tracked runs."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--since", default=None,
        help="ISO date filter on diagnosis_completed_at (e.g. 2026-08-21) -- "
             "use this PR's deploy date to exclude pre-instrumentation incidents",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of markdown")
    args = parser.parse_args()

    incidents = _load_diagnosed_incidents(args.since)
    summary = _summarize(incidents)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        _print_report(summary, args.since)
    return 0


if __name__ == "__main__":
    sys.exit(main())
