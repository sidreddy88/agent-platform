"""
Export ground truth for every merged ErrorClarityAgent observability PR into
a local regression dataset, for scripts/eval_error_clarity_regression.py to
replay against.

Sibling to scripts/export_diagnosis_regression_dataset.py, but a genuinely
different agent with a genuinely different notion of "correct" -- do not
merge the two datasets. DiagnosisAgent's ground truth is
diagnosis_affected_file, set directly on the incident. ErrorClarityAgent
does NOT persist which file(s) it touched on the incident record (only
clarity_pr_url/clarity_pr_number, clarity_summary) -- so this script fetches
the real affected file list from the actual merged PR via the GitHub API,
rather than reading a stored field.

Real motivation: found while investigating why the diagnosis-regression
dataset only had 6 of 11 real merged-fix incidents. The other 5 all have
outcome=="fix_merged" but no diagnosis_affected_file -- confirmed (by PR
title pattern "observability(...): add error handling for ..." and matching
merge timestamps) that all 5 are ErrorClarityAgent runs: confidence was
below the 0.70 auto-fix threshold, DiagnosisAgent escalated instead of
naming a file, and ErrorClarityAgent added logging/error-handling instead
of a real fix. Wrong agent to expect diagnosis_affected_file from -- these
need their own eval, not exclusion from the existing one.

Filter: outcome=="fix_merged" AND diagnosis_affected_file is NOT set AND
clarity_pr_url IS set. (A case with both fields set would mean a real fix
happened too, which shouldn't occur given ErrorClarityAgent only runs when
DiagnosisAgent already escalated -- treated as diagnosis-eval territory if
it ever does.)

Same production-only caveat as the diagnosis dataset: ground truth lives in
the live Postgres incidents table, not reproducible from a fresh clone. In
production this means running it via `aws ecs execute-command` inside the
container.

Output: app/evals/error_clarity_regression.jsonl (gitignored -- real
incident data, same handling as the diagnosis dataset).

Usage:
    python scripts/export_error_clarity_regression_dataset.py
    python scripts/export_error_clarity_regression_dataset.py --out /tmp/clarity.jsonl
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_DEFAULT_OUT = Path(__file__).resolve().parent.parent / "app" / "evals" / "error_clarity_regression.jsonl"

_PR_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _load_clarity_incidents() -> list[dict[str, Any]]:
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
        if blob.get("diagnosis_affected_file"):
            continue  # that's a real diagnosis case, belongs to the other dataset
        if not blob.get("clarity_pr_url"):
            continue  # no observability PR either -- nothing to check against
        incidents.append(blob)
    return incidents


async def _fetch_affected_files(pr_url: str) -> list[str]:
    from app.services.github import GitHubService

    m = _PR_URL_RE.search(pr_url or "")
    if not m:
        return []
    owner, repo, pr_number = m.group(1), m.group(2), int(m.group(3))
    github = GitHubService()
    diffs = await github.get_pr_diff(owner, repo, pr_number)
    return [d.filename for d in diffs]


def _to_regression_case(blob: dict[str, Any], affected_files: list[str]) -> dict[str, Any]:
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
            "clarity_pr_url": blob.get("clarity_pr_url"),
            "clarity_pr_number": blob.get("clarity_pr_number"),
            "affected_files": affected_files,
            "confidence": blob.get("confidence"),
        },
    }


async def _main_async(out_path: Path) -> int:
    blobs = _load_clarity_incidents()
    cases = []
    for b in blobs:
        files = await _fetch_affected_files(b.get("clarity_pr_url"))
        if not files:
            print(f"Skipping {b.get('id')}: could not fetch affected files for {b.get('clarity_pr_url')}")
            continue
        cases.append(_to_regression_case(b, files))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w") as f:
        for case in cases:
            f.write(json.dumps(case) + "\n")

    print(f"Exported {len(cases)} error-clarity regression case(s) to {out_path}")
    if not cases:
        print("No merged ErrorClarityAgent observability PRs found.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(_DEFAULT_OUT), help="Output JSONL path")
    args = parser.parse_args()
    return asyncio.run(_main_async(Path(args.out)))


if __name__ == "__main__":
    sys.exit(main())
