"""
Survey the real production CloudWatch history for AllInterviews
(/ecs/TaskAllInterviews, us-east-2, cross-account) for error diversity --
run BEFORE building any synthetic TriageAgent training data, to check
whether real severity diversity (P0/P1-shaped bursts, process crashes)
already exists in real production history that predates this pipeline's
own 4-month incident-tracking window (the `incidents` table only has 11
rows ever, because the automated pipeline has only been watching for ~4
months -- but the app itself has real traffic back to at least June 2024;
everything before that was a dev/test environment, confirmed by hand --
see docs/blog-drafts/ for how that was found).

Real motivation: don't build 500 synthetic cases before checking whether
real severity diversity was sitting in CloudWatch the whole time.

DECOUPLED BY DESIGN: each invocation scopes to its own --start/--end and
can use its own --filter-pattern. Results MERGE into the existing
checkpoint file rather than overwriting it, and every invocation is
recorded in a `runs` list (range + filter + when). This means: if
something interesting turns up in month 10 that suggests a better filter,
month 11 can be surveyed with the improved filter WITHOUT re-scanning
months already covered -- no monolithic single process, no forced
restart-from-scratch when the filter needs to change mid-survey (this is
exactly what happened once already: a bare "OOM" term matched inside
"BLOOM"/"ROOM" as noise, and fixing it meant restarting a single
all-32-months process from zero).

Within one invocation, still walks its range month-by-month (bounded,
resumable chunks) using filter-log-events, paginating within each month
via nextToken, checkpointing after every month.

What it surfaces per normalized error signature:
  - total occurrence count across all runs that have touched it
  - first/last seen timestamp
  - per-day occurrence histogram (for burst detection -- a genuine
    incident looks like a spike, not a steady trickle)
  - a few raw sample messages

Credentials: reads AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY directly from
.env (the cross-account AgentPlatformMonitor user already used by
production's own search_log_events() -- narrow IAM scope, FilterLogEvents
only, no DescribeLogGroups/StartQuery permission, confirmed by hand
before writing this script).

Usage:
    # Default filter, most-recent-first, whole range in one invocation:
    python scripts/survey_cloudwatch_history.py --start 2024-06-01 --end 2026-09-07

    # One month only, with a custom filter -- doesn't touch other months'
    # already-saved results:
    python scripts/survey_cloudwatch_history.py --start 2025-03-01 --end 2025-04-01 \\
        --filter-pattern '?"custom pattern" ?"another one"'
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

REGION = "us-east-2"
LOG_GROUP = "/ecs/TaskAllInterviews"
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
_OUT_PATH = Path(__file__).resolve().parent.parent / "docs" / "blog-drafts" / "cloudwatch_survey_results.json"

# Narrow net, deliberately -- a first pass with generic "Error"/"Exception"
# matched 500-1000+ lines per CloudWatch page (routine, individually-handled
# errors this app logs constantly during normal operation, not incidents).
# The goal here is specifically "did a real P0/P1-shaped event ever happen"
# -- process death / restart, fatal resource exhaustion -- which are rare
# and high-precision signals, not "every error this app ever logged."
#
# NOTE: bare "OOM" was tried and dropped -- it matches as an unanchored
# substring (CloudWatch filter patterns don't word-boundary bare terms),
# so it also caught "BLOOM", "ROOM", and random hex/request-id strings
# containing those letters. The genuine heap-exhaustion crashes it found
# were already covered by "out of memory" anyway -- zero real signal lost
# by dropping it, a lot of noise avoided.
DEFAULT_FILTER_PATTERN = (
    '?"app crashed" ?FATAL ?"out of memory" ?"heap out of memory" '
    '?SIGKILL ?"Cannot find module" ?"UnhandledPromiseRejectionWarning" '
    '?"MongoNetworkError" ?"ECONNREFUSED"'
)

# Per-month page cap -- safety valve against a genuinely runaway month
# (way above what any month has actually needed so far -- ~190 pages for
# a recent, high-traffic month), not a normal ceiling.
MAX_PAGES_PER_MONTH = 1000
# Deliberately gentle, not optimized for speed -- by choice, rather than
# hammering the API/service to finish faster. 1.5s between pages, 5s
# between months.
PAGE_SLEEP_SECONDS = 1.5
MONTH_SLEEP_SECONDS = 5.0
# Console noise control, separate from checkpoint safety -- checkpointing
# still happens every page (see checkpoint_cb in _survey_month), only
# console logging is throttled. A page with real matches always prints
# regardless of this cadence, so a real hit never gets buried in silence.
LOG_EVERY_N_PAGES = 20


def _load_creds() -> tuple[str, str]:
    access_key = secret_key = None
    for line in _ENV_PATH.read_text().splitlines():
        if line.startswith("AWS_ACCESS_KEY_ID="):
            access_key = line.split("=", 1)[1].strip()
        elif line.startswith("AWS_SECRET_ACCESS_KEY="):
            secret_key = line.split("=", 1)[1].strip()
    if not access_key or not secret_key:
        raise RuntimeError(f"AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY not found in {_ENV_PATH}")
    return access_key, secret_key


def _normalize(msg: str) -> str:
    """Collapse variable parts (hex ids, numbers, quoted values, ANSI
    color codes) so repeated occurrences of "the same" error group
    together instead of each being its own unique signature."""
    s = re.sub(r"\x1b\[[0-9;]*m", "", msg)          # strip ANSI color codes
    s = re.sub(r"[0-9a-fA-F]{8,}", "<HEX>", s)      # hex ids / hashes / ObjectIds
    s = re.sub(r'"[^"]{1,80}"', '"<STR>"', s)       # quoted values
    s = re.sub(r"\d+", "<NUM>", s)                   # any remaining numbers
    return s.strip()[:200]


def _month_windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    windows = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=30), end)
        windows.append((cur, nxt))
        cur = nxt
    return windows


def _survey_month(
    client: Any, start: datetime, end: datetime, filter_pattern: str,
    signatures: dict[str, dict[str, Any]], checkpoint_cb: Any = None,
) -> int:
    """Filter one month-sized window, paginating via nextToken. Mutates
    `signatures` in place. Returns total matched-line count this month.

    checkpoint_cb, if given, is called after every page (not just after
    the whole month) -- some months take 100+ pages, and an API/billing
    error or a killed process mid-month would otherwise lose everything
    found so far in that month, not just the current invocation."""
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    matched_this_month = 0
    next_token = None
    pages = 0

    throttle_retries = 0
    while True:
        pages += 1
        if pages > MAX_PAGES_PER_MONTH:
            print(f"    !! hit MAX_PAGES_PER_MONTH={MAX_PAGES_PER_MONTH} for "
                  f"{start.date()}..{end.date()} -- stopping this month early, "
                  f"results for this window are a partial sample, not complete.")
            break

        kwargs: dict[str, Any] = {
            "logGroupName": LOG_GROUP,
            "startTime": start_ms,
            "endTime": end_ms,
            "filterPattern": filter_pattern,
            "limit": 1000,
        }
        if next_token:
            kwargs["nextToken"] = next_token

        verbose = pages == 1 or pages % LOG_EVERY_N_PAGES == 0
        if verbose:
            print(f"      -> page {pages}: calling filter_log_events...", flush=True)
        t0 = time.monotonic()
        try:
            resp = client.filter_log_events(**kwargs)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ThrottlingException":
                throttle_retries += 1
                if throttle_retries > 10:
                    print("    !! throttled >10 times in a row, giving up on this "
                          "month early -- results are a partial sample.")
                    break
                print(f"    throttled (retry {throttle_retries}/10), backing off 5s...", flush=True)
                time.sleep(5)
                continue
            raise
        # Always log a page with real matches, even off-cadence -- a real
        # hit should never get buried in silence just because it landed
        # between two LOG_EVERY_N_PAGES checkpoints.
        if verbose or resp.get("events"):
            print(f"      <- page {pages} returned in {time.monotonic() - t0:.1f}s, "
              f"{len(resp.get('events', []))} events", flush=True)
        throttle_retries = 0

        events = resp.get("events", [])
        matched_this_month += len(events)
        for e in events:
            sig = _normalize(e["message"])
            day = datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).date().isoformat()
            entry = signatures.setdefault(sig, {
                "count": 0, "first_seen": None, "last_seen": None,
                "by_day": defaultdict(int), "samples": [],
            })
            entry["count"] += 1
            entry["by_day"][day] += 1
            ts_iso = datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).isoformat()
            if entry["first_seen"] is None or ts_iso < entry["first_seen"]:
                entry["first_seen"] = ts_iso
            if entry["last_seen"] is None or ts_iso > entry["last_seen"]:
                entry["last_seen"] = ts_iso
            if len(entry["samples"]) < 3:
                entry["samples"].append(e["message"][:300])

        if checkpoint_cb is not None:
            checkpoint_cb(pages)

        next_token = resp.get("nextToken")
        if not next_token:
            break
        time.sleep(PAGE_SLEEP_SECONDS)

    return matched_this_month


def _load_existing() -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Load the existing checkpoint, if any, converting by_day back to a
    defaultdict so _survey_month can keep mutating it. Returns
    (signatures, runs) -- runs is the history of *completed* windows, used
    to skip already-covered months on the next invocation. Also prints a
    resume note if the last invocation stopped mid-month."""
    if not _OUT_PATH.exists():
        return {}, []
    data = json.loads(_OUT_PATH.read_text())
    signatures: dict[str, dict[str, Any]] = {}
    for sig, entry in data.get("signatures", {}).items():
        e = dict(entry)
        e["by_day"] = defaultdict(int, e.get("by_day", {}))
        signatures[sig] = e

    in_progress = data.get("in_progress")
    if in_progress:
        print("=" * 70)
        print("RESUME NOTE: the last invocation stopped mid-month, not cleanly.")
        print(f"  Window:  {in_progress['start']} .. {in_progress['end']}")
        print(f"  Filter:  {in_progress['filter_pattern']}")
        print(f"  Reached: page {in_progress['pages_done']} before stopping "
              f"(at {in_progress['last_checkpoint_at']})")
        print(f"  Everything found in those {in_progress['pages_done']} pages is "
              f"already merged into signatures below -- re-running with the SAME "
              f"--start/--end/--filter-pattern will re-survey only this one "
              f"partial window (already-completed months are skipped automatically).")
        print(f"  Exact resume command:")
        print(f"    python scripts/survey_cloudwatch_history.py "
              f"--start {in_progress['start']} --end {in_progress['end']} "
              f"--filter-pattern '{in_progress['filter_pattern']}'"
              + (" --oldest-first" if in_progress.get("oldest_first") else ""))
        print("=" * 70 + "\n")

    return signatures, data.get("runs", [])


def _save_checkpoint(
    signatures: dict[str, dict[str, Any]], runs: list[dict[str, Any]],
    in_progress: dict[str, Any] | None = None,
) -> None:
    """in_progress, when set, is exactly what a resume note needs: which
    window is currently (or was, if this is the last write before a
    crash) being surveyed, how far it got, and the filter used. Cleared
    (set to None) once a month actually finishes -- a `None` here on
    disk means "last invocation ended clean, nothing partial to resume.\""""
    serializable = {}
    for sig, entry in signatures.items():
        e = dict(entry)
        e["by_day"] = dict(entry["by_day"])
        serializable[sig] = e
    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(json.dumps({
        "distinct_signatures": len(signatures),
        "runs": runs,
        "in_progress": in_progress,
        "signatures": serializable,
    }, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="Survey start date (YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="Survey end date (YYYY-MM-DD), default now")
    parser.add_argument("--filter-pattern", default=DEFAULT_FILTER_PATTERN,
                         help="CloudWatch filter pattern for this invocation only -- "
                              "doesn't affect already-saved results from other runs")
    parser.add_argument("--oldest-first", action="store_true",
                         help="Survey oldest-to-newest instead of the default newest-first")
    parser.add_argument("--sample-step-months", type=int, default=None,
                         help="Instead of every consecutive month, survey only every Nth "
                              "month (e.g. 4 = one month, skip 3, next month, skip 3, ...). "
                              "Consecutive-month scanning tends to just re-find occurrences "
                              "of the same currently-active failure modes; widely-spaced "
                              "sampling across a long history is more likely to surface "
                              "genuinely distinct ones from different eras of the app.")
    args = parser.parse_args()

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = (
        datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
        if args.end else datetime.now(timezone.utc)
    )

    access_key, secret_key = _load_creds()
    client = boto3.client(
        "logs", region_name=REGION,
        aws_access_key_id=access_key, aws_secret_access_key=secret_key,
        config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
    )

    signatures, runs = _load_existing()
    if signatures:
        print(f"Loaded existing checkpoint: {len(signatures)} signatures from "
              f"{len(runs)} prior run(s). This invocation MERGES into it.\n")

    windows = _month_windows(start, end)
    if not args.oldest_first:
        windows = list(reversed(windows))
    order_desc = "oldest first" if args.oldest_first else "most recent first"
    if args.sample_step_months:
        windows = windows[::args.sample_step_months]
        print(f"Surveying {LOG_GROUP} ({REGION}) from {start.date()} to {end.date()}, "
              f"{order_desc}, sampling one month every {args.sample_step_months} "
              f"({len(windows)} sample windows).")
    else:
        print(f"Surveying {LOG_GROUP} ({REGION}) from {start.date()} to {end.date()} "
              f"in {len(windows)} ~30-day windows, {order_desc}.")
    print(f"Filter: {args.filter_pattern}\n")

    # Skip windows already fully covered by a prior run with this exact
    # filter -- without this, re-running after a crash (or just running
    # the same command twice) would re-scan and double-count everything.
    completed = {
        (r["start"], r["end"], r["filter_pattern"])
        for r in runs
    }

    total_matched = 0
    for i, (w_start, w_end) in enumerate(windows, 1):
        key = (w_start.date().isoformat(), w_end.date().isoformat(), args.filter_pattern)
        if key in completed:
            print(f"[{i}/{len(windows)}] {w_start.date()} .. {w_end.date()} -- "
                  f"already covered with this filter, skipping.", flush=True)
            continue

        print(f"[{i}/{len(windows)}] {w_start.date()} .. {w_end.date()} ...", flush=True)

        # Checkpoint after every PAGE, not just after the whole month --
        # some months take 100+ pages, and an AWS API/billing error or a
        # killed process mid-month would otherwise lose everything found
        # so far in that month, not just this invocation. `runs` doesn't
        # get this window's "completed" entry until the month actually
        # finishes below, so a crash here leaves `in_progress` populated
        # on disk -- the skip-set above only matches `runs`, so a resume
        # correctly re-surveys just this one partial window, not
        # everything, and not silently claiming it as done either.
        def _mid_month_checkpoint(pages_done: int) -> None:
            _save_checkpoint(signatures, runs, in_progress={
                "start": w_start.date().isoformat(), "end": w_end.date().isoformat(),
                "filter_pattern": args.filter_pattern, "pages_done": pages_done,
                "oldest_first": args.oldest_first,
                "last_checkpoint_at": datetime.now(timezone.utc).isoformat(),
            })

        try:
            n = _survey_month(client, w_start, w_end, args.filter_pattern,
                               signatures, checkpoint_cb=_mid_month_checkpoint)
        except Exception as exc:
            print(f"\n!! Unhandled error mid-month ({w_start.date()}..{w_end.date()}): {exc}")
            print("   Everything found up to the last completed page is already "
                  "saved (per-page checkpointing) -- the in_progress note in "
                  f"{_OUT_PATH.name} has the exact resume command. Re-running it "
                  "will re-survey only this one partial month; already-completed "
                  "months are skipped automatically.")
            raise

        total_matched += n
        print(f"    -> {n} matched lines this window, "
              f"{len(signatures)} distinct signatures so far", flush=True)
        runs_so_far = runs + [{
            "start": w_start.date().isoformat(), "end": w_end.date().isoformat(),
            "filter_pattern": args.filter_pattern,
            "surveyed_at": datetime.now(timezone.utc).isoformat(),
        }]
        # Month finished clean -- clear in_progress (None = nothing partial).
        _save_checkpoint(signatures, runs_so_far, in_progress=None)
        runs = runs_so_far
        time.sleep(MONTH_SLEEP_SECONDS)

    print(f"\nDone this invocation. {total_matched} total matched lines this run, "
          f"{len(signatures)} distinct signatures overall.")
    print(f"Full results written to {_OUT_PATH}")

    ranked = sorted(signatures.items(), key=lambda kv: kv[1]["count"], reverse=True)
    print("\nTop 15 signatures by total count (across all runs so far):")
    for sig, entry in ranked[:15]:
        max_day_count = max(entry["by_day"].values()) if entry["by_day"] else 0
        print(f"  {entry['count']:>6}  (max {max_day_count}/day)  "
              f"{entry['first_seen'][:10]}..{entry['last_seen'][:10]}  {sig[:100]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
