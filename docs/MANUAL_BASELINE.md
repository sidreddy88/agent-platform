# Manual Baseline — Pre-Agent MTTR Estimate

The agent platform's MTTR is meaningless without a "before" number. This
doc is the honest pre-agent estimate to pair with the dashboard metrics
in interviews.

## Method

Take 2–3 representative production incidents from before the agent
platform was running. For each, estimate three timestamps:

1. **First occurrence** — when the bug first appeared in production logs.
2. **Engineer noticed** — when a human first realised something was
   wrong (user complaint, internal use, or routine log scan).
3. **Fix shipped** — when a fix landed in production.

Compute `(fix shipped − first occurrence)` per incident, then average.

## Reference incidents

Fill in actual numbers based on real incidents from the TargetApp
production fleet. The two below are placeholders pulled from session
memory — replace with real estimates and dates before quoting these in
an interview.

| Incident | First occurred | Engineer noticed | Fix shipped | Pre-agent MTTR |
|---|---|---|---|---|
| Sharp HEIF codec failure on `.jpg` upload | _e.g. 2026-03-14 09:00 UTC_ | _e.g. 2026-03-14 14:30 UTC_ | _e.g. 2026-03-15 11:00 UTC_ | _~26h_ |
| S3 NoSuchKey on `cr/tmp/...` cleanup path | _e.g. 2026-02-08 10:00 UTC_ | _e.g. 2026-02-08 12:15 UTC_ | _e.g. 2026-02-08 19:00 UTC_ | _~9h_ |
| _(third incident if you have one)_ | _..._ | _..._ | _..._ | _..._ |

**Average pre-agent MTTR (estimated):** _~XX hours_

## Notes on honesty

When asked in interviews:

- Be explicit these are **estimates from memory**, not telemetry. Real
  pre-agent observability didn't exist for these incidents — that's
  literally why the platform was built.
- The huge variance (9h vs 26h above) is the point. Pre-agent MTTR is
  bimodal — fast if it hit during business hours and got user reports,
  slow if it hit overnight or in a low-traffic flow.
- Most pre-agent fix time was **detection lag**, not engineering time.
  Once the engineer was looking at the bug, fixes typically took 1–3
  hours. The 9–26h is dominated by the gap between first occurrence and
  noticing.

## After

From the dashboard + `scripts/measure_mttr.py`:

- **System MTTD** (push-based ingest, after Tier 1.1a): single-digit
  minutes from alarm transition to platform pickup.
- **Agent pipeline time:** ~6 min from event ingested → PR ready for
  review.
- **Wall-clock MTTR:** ~1.5h, the bulk of which is the human approval
  gate (deliberate, not a bottleneck).
- **Cost per incident:** ~$0.12.

## Interview line

*"Before the agent platform, average MTTR on these incidents was roughly
\<XX>h — and most of that was detection lag, not engineering time. After:
the agent has a fix PR ready in 6 minutes from alarm. I keep a 1.5h
wall-clock MTTR because I require human approval for HIGH/CRITICAL
changes — that's a deliberate gate, not a bottleneck."*
