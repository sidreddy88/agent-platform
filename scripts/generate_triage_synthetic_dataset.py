"""
Generate synthetic TriageAgent training/eval cases: synthetic INPUTS,
REAL model judgments. Not fabricated labels -- every input is fed through
the real TriageAgent (real Haiku call), and only cases whose actual
output matches the intended target category are kept.

Real motivation: golden_dataset.jsonl's 512 real cases are only ~14-16
genuinely unique scenarios (97% just 2 error types, 511/512 rows are P2,
zero P0/P3 ever). The CloudWatch survey found real severity diversity for
P0/P1 (see docs/blog-drafts/cloudwatch_survey_results.json) but nothing
for P3/noise -- those categories structurally don't have enough real
history to sample from. This generates the missing coverage honestly:
synthetic scenario construction + genuine model labeling, not hand-written
labels.

Mechanism (mirrors app/services/eval_runner.py's existing _EvalAWSStub /
_EvalStoreStub pattern, but CONTROLLABLE per-case instead of hardcoded --
the existing stubs always return "5 occurrences" / "no duplicate", which
can't target different severities or the duplicate category on purpose):
  1. A template supplies an input shape (error_type, message, service)
     grounded in real production taxonomy (verified this session --
     heap OOM, ECONNREFUSED, CastError, etc. -- nothing invented) plus a
     target category to aim for.
  2. _ControllableAWSStub.search_log_events returns exactly as many
     synthetic events as the template's target occurrence count implies,
     so TriageAgent's real get_occurrence_count tool call sees a genuine
     (albeit synthetic) count -- not fabricated confidence, an honestly
     constructed input.
  3. _ControllableStoreStub.get_pr_for_resource returns a fake PR URL
     when the template targets `duplicate`, None otherwise -- TriageAgent
     is hard-constrained in its own prompt to only output "duplicate" when
     this tool says DUPLICATE, so this is the one category we can target
     with certainty.
  4. The REAL TriageAgent (real Haiku call) runs against the synthetic
     input + these mocked tools and produces its own genuine judgment.
  5. Keep only cases where the real output matches the template's
     intended category -- mismatches are discarded, not corrected. This
     is real, per your own earlier framing: synthetic inputs, real
     judgments, not hand-fabricated labels.

Severity heuristic (verified against the real prompt in app/agents/
triage.py, not assumed):
  P0 -- impact language only ("service down/data loss/blocking all
        users"), NOT frequency-gated -- can fire at any occurrence count.
  P1 -- >100/24h
  P2 -- 5-100/24h
  P3 -- <5/24h

Usage:
    python scripts/generate_triage_synthetic_dataset.py --pilot
    python scripts/generate_triage_synthetic_dataset.py --category P0 --count 50
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_OUT_PATH = Path(__file__).resolve().parent.parent / "app" / "evals" / "triage_synthetic_dataset.jsonl"


# ---------------------------------------------------------------------------
# Controllable stubs -- same shape as eval_runner.py's _EvalAWSStub /
# _EvalStoreStub, but parameterized per-case instead of hardcoded.
# ---------------------------------------------------------------------------

class _ControllableAWSStub:
    """search_log_events is what DiagnosisAgent^H^H^H^H TriageAgent's
    get_occurrence_count tool actually calls (app/agents/triage.py) --
    NOT get_log_occurrences, which is what the existing (hardcoded)
    _EvalAWSStub implements. That mismatch is harmless for the existing
    golden_dataset.jsonl cases (none of them set log_group, so the model
    is instructed to skip calling it at all) but would matter here,
    since we specifically want occurrence count to drive severity."""

    def __init__(self, occurrence_count: int):
        self._occurrence_count = occurrence_count

    def search_log_events(self, log_group, filter_pattern, minutes=5, limit=100, region=None):
        return [
            {"timestamp": f"2026-01-01T00:00:{i:02d}Z", "stream": "synthetic",
             "message": f"synthetic occurrence {i} of {filter_pattern}"}
            for i in range(min(self._occurrence_count, limit))
        ][: self._occurrence_count if self._occurrence_count <= limit else limit]

    def __getattr__(self, name):
        return lambda *a, **k: None


class _ControllableStoreStub:
    def __init__(self, duplicate_pr_url: str | None):
        self._duplicate_pr_url = duplicate_pr_url

    def get_pr_for_resource(self, *args, **kwargs):
        return self._duplicate_pr_url

    def set_pr_for_resource(self, *args, **kwargs):
        pass


# ---------------------------------------------------------------------------
# Templates -- every error_type/message here is grounded in real production
# taxonomy verified this session (golden_dataset.jsonl or the CloudWatch
# survey), nothing invented.
# ---------------------------------------------------------------------------

@dataclass
class Template:
    error_type: str
    message: str
    service: str
    category: str          # "P0" | "P1" | "P2" | "P3" | "noise" | "duplicate"
    occurrence_count: int
    log_group: str = "ecs/ContainerProcess/synthetic"


TEMPLATES: list[Template] = [
    # --- P0: impact language, not frequency-gated. Pilot round 1 (moderate
    # counts, implied-but-not-blunt impact language) mostly got rated P2 --
    # the model seemed to read "recurring at a moderate rate" as routine
    # rather than catastrophic. Round 2: much blunter language matching the
    # prompt's exact P0 wording ("service down/data loss/blocking all
    # users") + LOW counts framed as "just started" (an acute fresh outage
    # reads more P0-shaped than a moderate ongoing rate). ---
    Template("HEAP_OUT_OF_MEMORY",
              "FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory. "
              "Application process has crashed and is not responding. The entire site is down -- "
              "100% of requests are failing for all users right now.",
              "allinterviews-api", "P0", occurrence_count=5),
    Template("MONGO_CONNECTION_REFUSED",
              "MongooseServerSelectionError: connect ECONNREFUSED 127.0.0.1:27017. Database is "
              "completely unreachable. Every single API request is failing right now -- this is a "
              "full outage, no functionality works for any user.",
              "allinterviews-api", "P0", occurrence_count=8),
    Template("MODULE_NOT_FOUND",
              "Error: Cannot find module '../../models/InterviewUser'. The application process "
              "cannot start at all. The entire site is down for all users -- complete outage, not "
              "a partial degradation.",
              "allinterviews-api", "P0", occurrence_count=3),
    Template("PROCESS_KILLED",
              "node completed with null:SIGKILL. The container is being forcibly terminated and "
              "cannot stay running. The site is completely down and inaccessible to all users "
              "right now.",
              "allinterviews-api", "P0", occurrence_count=4),

    # --- P1: same real incident shapes, framed as degraded-but-serving,
    # driven into >100/24h by occurrence count ---
    Template("HEAP_OUT_OF_MEMORY",
              "FATAL ERROR: Ineffective mark-compacts near heap limit Allocation failed - "
              "JavaScript heap out of memory (recurring, app auto-restarts)",
              "allinterviews-worker", "P1", occurrence_count=180),
    Template("ECONNREFUSED",
              "AxiosError: connect ECONNREFUSED -- downstream API calls failing intermittently",
              "allinterviews-api", "P1", occurrence_count=140),
    Template("S3_NO_SUCH_KEY",
              "NoSuchKey: The specified key does not exist -- repeated upload failures affecting many users",
              "allinterviews-api", "P1", occurrence_count=210),

    # --- P2: real, moderate-frequency recurring failures ---
    Template("S3_NO_SUCH_KEY", "NoSuchKey: The specified key does not exist",
              "allinterviews-api", "P2", occurrence_count=40),
    Template("NULL_PTR", "NullPointerException", "allinterviews-api", "P2", occurrence_count=35),
    Template("CASTERROR",
              'CastError: Cast to Number failed for value "abc123" (type string) at path "previewCode"',
              "allinterviews-api", "P2", occurrence_count=25),
    Template("AXIOSERROR", "Error-> Image ADA/LLM analysis failed", "allinterviews-api", "P2",
              occurrence_count=15),
    Template("ECS_ERROR", "moveAndRemoveFileFromS3 error NoSuchKey", "allinterviews-api", "P2",
              occurrence_count=30),
    # Moved here from P3 after re-testing: the model consistently (0/3, both
    # pilot rounds) rated this P2 regardless of "just a warning" framing --
    # a deprecation warning genuinely reads as "needs a fix eventually, not
    # urgent" to real judgment, closer to P2 than P3. Reclassifying the
    # target rather than fighting a reasonable model judgment.
    Template("DEPRECATIONWARNING",
              "(node:30) [MONGOOSE] DeprecationWarning: Mongoose: the `strictQuery` option",
              "allinterviews-api", "P2", occurrence_count=3),

    # --- P3: real, rare/low-impact. Pilot round 1 (low count + implied
    # framing) was inconsistent -- the model weighed the CONTENT of the
    # error (e.g. "SyntaxError" sounds like a real bug worth fixing) over
    # the low count, bumping several to P2 despite count clearly <5.
    # Round 2: explicit "isolated, one-off, low impact" language on top
    # of the low count, not relying on count alone. ---
    Template("SYNTAXERROR",
              "Failed to parse model JSON: SyntaxError: Unexpected token. Isolated one-off "
              "occurrence affecting a single request; no other users impacted, low priority.",
              "allinterviews-api", "P3", occurrence_count=2),
    Template("TOKENEXPIREDERROR",
              "authenticateToken jwt.verify error TokenExpiredError: jwt expired. Single isolated "
              "occurrence, very low impact, not a pattern.",
              "allinterviews-api", "P3", occurrence_count=1),
    Template("APP_CRASHED",
              "[nodemon] app crashed - waiting for file changes before starting. Isolated one-time "
              "restart that self-recovered immediately; no lasting impact, very low priority.",
              "allinterviews-api", "P3", occurrence_count=1),

    # --- noise: transient/expected framing. Round 1's "transient" wasn't
    # unambiguous enough -- HEALTH_CHECK_TIMEOUT kept landing as real/P3
    # instead of noise. Round 2: explicit "this is expected behavior, not
    # a bug, no action needed" framing. ---
    Template("S3_NO_SUCH_KEY",
              "S3 throws NoSuchKey on missing key. This is expected, routine behavior during normal "
              "cleanup operations -- not a bug, no action needed, standard operational noise.",
              "allinterviews-api", "noise", occurrence_count=1),
    Template("HEALTH_CHECK_TIMEOUT",
              "Health check timeout, but the load balancer's retry succeeded immediately afterward. "
              "This is expected, routine transient network jitter -- not a real problem, no action "
              "needed, false alarm.",
              "allinterviews-api", "noise", occurrence_count=2),
    Template("TOKENEXPIREDERROR",
              "authenticateToken jwt.verify error TokenExpiredError: jwt expired. This is completely "
              "normal and expected when a user's session naturally times out -- not a bug, no code "
              "change needed, standard expected behavior.",
              "allinterviews-api", "noise", occurrence_count=1),

    # --- duplicate: any real error type; the mocked store is what
    # actually drives this category, not the input content ---
    Template("CASTERROR", 'CastError: Cast to Number failed for value "xyz789" (type string) at path "previewCode"',
              "allinterviews-api", "duplicate", occurrence_count=10),
    Template("ECS_ERROR", "moveAndRemoveFileFromS3 error NoSuchKey: The specified key does not exist",
              "allinterviews-api", "duplicate", occurrence_count=8),
    Template("SYNTAXERROR", "Failed to parse model JSON: SyntaxError: Unexpected token",
              "allinterviews-api", "duplicate", occurrence_count=5),
]


def _build_event(template: Template, jitter_seed: int) -> dict[str, Any]:
    """Build a synthetic ErrorEvent input dict, jittered slightly per
    instance (occurrence count +/-20%) so repeated generations from the
    same template aren't byte-identical."""
    rng = random.Random(jitter_seed)
    jittered_count = max(0, int(template.occurrence_count * rng.uniform(0.8, 1.2)))
    return {
        "error_type": template.error_type,
        "title": f"{template.error_type} in {template.service}",
        "description": template.message,
        "service": template.service,
        "source": "cloudwatch",
        "metadata": {"log_group": template.log_group, "pattern": template.error_type},
    }, jittered_count


async def _run_one(template: Template, jitter_seed: int, has_existing_pr: bool) -> dict[str, Any]:
    from app.agents.triage import TriageAgent
    from app.models.events import ErrorEvent, EventSource

    event_data, occurrence_count = _build_event(template, jitter_seed)
    event = ErrorEvent(
        source=EventSource.APPLICATION,
        error_type=event_data["error_type"], title=event_data["title"],
        description=event_data["description"], service=event_data["service"],
        metadata=event_data["metadata"],
    )

    aws_stub = _ControllableAWSStub(occurrence_count=occurrence_count)
    pr_url = f"https://github.com/example/repo/pull/{jitter_seed % 9999}" if has_existing_pr else None
    store_stub = _ControllableStoreStub(duplicate_pr_url=pr_url)

    agent = TriageAgent(aws=aws_stub, store=store_stub)
    result = await agent.triage(event)

    intended = template.category
    if intended in ("P0", "P1", "P2", "P3"):
        matched = result.decision == "real" and result.severity == intended
    else:
        matched = result.decision == intended

    return {
        "id": f"synth_{uuid.uuid4().hex[:8]}",
        "input": {
            "error_type": template.error_type,
            "title": event_data["title"],
            "description": template.message,
            "service": template.service,
            "source": "cloudwatch",
            "log_group": template.log_group,
            "occurrence_count": occurrence_count,
            "has_existing_pr": has_existing_pr,
        },
        "output": {
            "decision": result.decision,
            "severity": result.severity,
            "reasoning": result.reasoning,
        },
        "metadata": {
            "tags": ["synthetic-balanced"],
            "intended_category": intended,
            "matched": matched,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    }


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot", action="store_true",
                         help="Run 2 instances per template (small validation batch)")
    parser.add_argument("--count-per-template", type=int, default=None,
                         help="Override instances per template (default: pilot=2, else 10)")
    parser.add_argument("--only-category", default=None,
                         help="Only run templates targeting this category (P0/P1/P2/P3/noise/duplicate) "
                              "-- for re-testing a fixed category without re-running everything else")
    args = parser.parse_args()

    per_template = args.count_per_template or (2 if args.pilot else 10)
    templates = (
        [t for t in TEMPLATES if t.category == args.only_category]
        if args.only_category else TEMPLATES
    )

    kept: list[dict[str, Any]] = []
    discarded = 0
    seed = 0

    for template in templates:
        for _ in range(per_template):
            seed += 1
            has_pr = template.category == "duplicate"
            print(f"[{seed}] {template.error_type} -> targeting {template.category} "
                  f"(count={template.occurrence_count}, dup={has_pr}) ...", flush=True)
            try:
                case = await _run_one(template, seed, has_pr)
            except Exception as exc:
                print(f"    ERROR: {exc}", flush=True)
                continue
            status = "KEPT" if case["metadata"]["matched"] else "DISCARD"
            print(f"    -> {status}: decision={case['output']['decision']} "
                  f"severity={case['output']['severity']}", flush=True)
            if case["metadata"]["matched"]:
                kept.append(case)
            else:
                discarded += 1

    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _OUT_PATH.open("a") as f:
        for case in kept:
            f.write(json.dumps(case) + "\n")

    print(f"\nDone. Kept {len(kept)}, discarded {discarded} "
          f"(match rate: {len(kept)/(len(kept)+discarded):.0%})")
    print(f"Appended to {_OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
