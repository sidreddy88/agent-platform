"""
MTTR decomposition — split incident time into agent-bound vs human-bound
segments so the headline "MTTR is 1.5h" can be reframed as
"6 minutes of agent work + ~84 minutes of human approval".

Runs read-only against agent_platform.db. Prints a markdown summary
table to stdout. Pipe into a doc, paste into the README, or save to
a file.

Usage:
    python scripts/measure_mttr.py
    python scripts/measure_mttr.py --json    # machine-readable output
    python scripts/measure_mttr.py --since 2026-04-01

Schema reminder (from app/services/database.py):
    incidents.detected_at          → wall-clock start
    incidents.data (JSON)          → resolved_at, pr_url, status
    agent_runs.agent_name          → which agent
    agent_runs.started_at          → ISO timestamp
    agent_runs.completed_at        → ISO timestamp (nullable)
    agent_runs.duration_ms         → REAL
    agent_runs.cost_usd            → REAL
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "agent_platform.db"

# Agent runs that are agent-bound time. Anything else is either human-bound
# (the approval gate) or out-of-band (monitor generation on PR merge).
AGENT_PIPELINE = (
    "TriageAgent",
    "DiagnosisAgent",
    "FixGenerationAgent",
    "CodeReviewAgent",
    "ErrorClarityAgent",
    "MergeDecisionAgent",
)


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _minutes(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end:
        return None
    return round((end - start).total_seconds() / 60.0, 1)


def _per_incident_decomposition(conn: sqlite3.Connection, since: str | None) -> list[dict[str, Any]]:
    """Build one row per incident with per-segment minutes."""
    if since:
        rows = conn.execute(
            "SELECT id, status, detected_at, data FROM incidents "
            "WHERE detected_at >= ? ORDER BY detected_at ASC",
            (since,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, status, detected_at, data FROM incidents "
            "ORDER BY detected_at ASC"
        ).fetchall()

    summaries: list[dict[str, Any]] = []
    for inc in rows:
        inc_id = inc["id"]
        detected_at = _parse_iso(inc["detected_at"])
        try:
            blob = json.loads(inc["data"])
        except (json.JSONDecodeError, TypeError):
            blob = {}
        resolved_at = _parse_iso(blob.get("resolved_at"))
        pr_url = blob.get("pr_url") or ""

        runs = conn.execute(
            "SELECT agent_name, started_at, completed_at, duration_ms, cost_usd "
            "FROM agent_runs WHERE incident_id = ? "
            "ORDER BY started_at ASC",
            (inc_id,),
        ).fetchall()

        # Per-agent time (sum, in case an agent is invoked multiple times).
        per_agent_min: dict[str, float] = defaultdict(float)
        per_agent_cost: dict[str, float] = defaultdict(float)
        first_run: datetime | None = None
        last_run: datetime | None = None
        for r in runs:
            dur_ms = r["duration_ms"] or 0
            per_agent_min[r["agent_name"]] += round(dur_ms / 60_000.0, 2)
            per_agent_cost[r["agent_name"]] += float(r["cost_usd"] or 0)
            started = _parse_iso(r["started_at"])
            completed = _parse_iso(r["completed_at"]) or started
            if started and (first_run is None or started < first_run):
                first_run = started
            if completed and (last_run is None or completed > last_run):
                last_run = completed

        agent_bound_min = round(sum(per_agent_min.values()), 1)
        agent_pipeline_min = round(
            sum(v for k, v in per_agent_min.items() if k in AGENT_PIPELINE), 1
        )
        total_cost = round(sum(per_agent_cost.values()), 4)

        # Wall-clock from detection → resolution. Human-bound = wall - agent_bound.
        wall_min = _minutes(detected_at, resolved_at)
        human_bound_min = (
            round(wall_min - agent_bound_min, 1)
            if wall_min is not None
            else None
        )

        summaries.append({
            "incident_id": inc_id,
            "status": inc["status"],
            "detected_at": inc["detected_at"],
            "resolved_at": blob.get("resolved_at"),
            "pr_url": pr_url,
            "wall_min": wall_min,
            "agent_bound_min": agent_bound_min,
            "agent_pipeline_min": agent_pipeline_min,
            "human_bound_min": human_bound_min,
            "per_agent_min": dict(per_agent_min),
            "total_cost_usd": total_cost,
            "first_agent_run": first_run.isoformat() if first_run else None,
            "last_agent_run": last_run.isoformat() if last_run else None,
        })
    return summaries


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Average across resolved incidents only — partial incidents skew badly."""
    resolved = [r for r in rows if r["status"] == "resolved" and r["wall_min"] is not None]
    n = len(resolved)
    if n == 0:
        return {"resolved_count": 0}

    avg = lambda field: round(sum(r[field] for r in resolved if r[field] is not None) / n, 1)
    avg_cost = round(sum(r["total_cost_usd"] for r in resolved) / n, 4)

    per_agent_avg: dict[str, float] = defaultdict(float)
    for r in resolved:
        for agent, m in r["per_agent_min"].items():
            per_agent_avg[agent] += m
    per_agent_avg = {k: round(v / n, 2) for k, v in per_agent_avg.items()}

    return {
        "resolved_count": n,
        "avg_wall_min": avg("wall_min"),
        "avg_agent_bound_min": avg("agent_bound_min"),
        "avg_agent_pipeline_min": avg("agent_pipeline_min"),
        "avg_human_bound_min": avg("human_bound_min"),
        "avg_cost_usd": avg_cost,
        "avg_per_agent_min": per_agent_avg,
    }


def _print_markdown(rows: list[dict[str, Any]], totals: dict[str, Any]) -> None:
    n = totals.get("resolved_count", 0)
    if n == 0:
        print("No resolved incidents found.\n"
              "Either the platform hasn't shipped any fixes yet, or `--since` filtered them all out.")
        return

    print(f"# MTTR decomposition (resolved incidents, N={n})")
    print()
    print(f"- **Avg wall-clock MTTR:**     {totals['avg_wall_min']} min "
          f"(~{round(totals['avg_wall_min'] / 60.0, 2)}h)")
    print(f"- **Avg agent pipeline time:** {totals['avg_agent_pipeline_min']} min "
          "← *headline number for interviews*")
    print(f"- **Avg human-bound time:**    {totals['avg_human_bound_min']} min "
          "(approval gate, deliberate)")
    print(f"- **Avg cost / incident:**     ${totals['avg_cost_usd']}")
    print()
    print("## Per-agent average (minutes)")
    print()
    print("| Agent | Avg minutes |")
    print("|---|---|")
    for agent, m in sorted(totals["avg_per_agent_min"].items(), key=lambda x: -x[1]):
        print(f"| {agent} | {m} |")
    print()

    print("## Per-incident detail")
    print()
    print("| Incident | Status | Wall (min) | Agent (min) | Human (min) | Cost ($) | PR |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        wall = r["wall_min"] if r["wall_min"] is not None else "—"
        human = r["human_bound_min"] if r["human_bound_min"] is not None else "—"
        pr = r["pr_url"].rsplit("/", 1)[-1] if r["pr_url"] else "—"
        print(f"| `{r['incident_id'][:8]}` | {r['status']} | {wall} | "
              f"{r['agent_bound_min']} | {human} | {r['total_cost_usd']} | {pr} |")

    # Tracking-completeness check: warn if pipeline agents that should have
    # fired didn't show up in agent_runs for resolved incidents. Common cause
    # is that FixGenerationAgent / CodeReviewAgent runs aren't being
    # attributed to their incidents (incident_id missing).
    expected = {"TriageAgent", "DiagnosisAgent", "FixGenerationAgent", "CodeReviewAgent"}
    seen = set(totals["avg_per_agent_min"].keys())
    missing = sorted(expected - seen)
    if missing:
        print()
        print(f"> ⚠ Agent-bound total may be undercounted: no agent_runs rows for "
              f"{', '.join(missing)} attributed to resolved incidents. "
              f"The dashboard's pipeline-time metric pulls from a different source "
              f"(latency_tracker) and is more accurate. To close this gap, ensure "
              f"these agents pass `incident_id` when calling `agent_tracker.record_*`.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH), help="path to agent_platform.db")
    parser.add_argument("--since", default=None,
                        help="ISO date filter on detected_at (e.g. 2026-04-01)")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of markdown")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        print(f"DB not found at {db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        rows = _per_incident_decomposition(conn, args.since)
    finally:
        conn.close()

    totals = _aggregate(rows)

    if args.json:
        print(json.dumps({"totals": totals, "incidents": rows}, indent=2, default=str))
    else:
        _print_markdown(rows, totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
