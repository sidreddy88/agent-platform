"""
TriageAgent — classifies ErrorEvents as real / noise / duplicate and assigns P0–P3 severity.

Model: Claude Haiku (fast, cheap — runs on every alert)

Flow:
  1. check_duplicate_pr   → is there already an open PR for this error type?
  2. get_occurrence_count → how many times has this fired in the last 24 h?
  3. One forced-tool-use LLM call → structured triage decision

check_duplicate_pr/get_occurrence_count take arguments fully determined by the
incoming ErrorEvent — no model judgment is needed to decide what to pass them
— so triage() calls them directly rather than routing them through an
LLM-driven ReAct turn just to have the model copy the same values into an
Action Input. They're still registered via self.register_tool() below, so
the exact same closures remain independently testable via the tool registry
(see tests/test_search_log_events_region.py, which exercises
get_occurrence_count that way to protect a real past region bug).

The final classification is a forced tool call
(LLMService.complete_structured, tool_choice locked to submit_triage) rather
than JSON-in-prose extracted with a regex: every field is guaranteed
present, decision/severity/blast_radius are guaranteed one of their declared
enum values, at the API level. This replaced a regex-extract + json.loads +
hardcoded-fallback-default parser that silently degraded to
decision="real"/severity="P2" on any malformed response — an undetectable
failure mode this shape eliminates structurally rather than defending
against after the fact.

Trade-off, disclosed not hidden: bypassing BaseAgent.run() means TriageAgent
no longer gets @trace_agent's Langfuse agent-level span (a ReAct-loop-
specific decorator with no equivalent for a single forced-tool-use call).
agent_tracker's start/complete/fail calls are preserved manually below since
they back the live AgentHealthPanel — Langfuse tracing is optional/best-
effort everywhere in this codebase (see CLAUDE.md), so losing one agent's
span there is a minor, acceptable trade against removing a whole
non-deterministic parsing layer.

Output:
  {
    "decision": "real" | "noise" | "duplicate",
    "severity": "P0" | "P1" | "P2" | "P3",
    "blast_radius": "single_service" | "multi_service" | "unknown",
    "occurrences_24h": <int>,
    "duplicate_pr": "<url or null>",
    "reasoning": "<one sentence>"
  }
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from app.agents.base import BaseAgent, incident_id_ctx
from app.core.config import settings
from app.models.events import ErrorEvent
from app.services.aws import AWSError, AWSService
from app.services.incident_store import incident_store as _default_store
from app.services.ipi_guard import scan_for_injection
from app.services.llm import HAIKU_MODEL, LLMService
from app.services.output_validator import check_leaked_markers

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class TriageResult:
    decision: str           # "real" | "noise" | "duplicate"
    severity: str           # "P0" | "P1" | "P2" | "P3"
    blast_radius: str       # "single_service" | "multi_service" | "unknown"
    occurrences_24h: int
    duplicate_pr: str | None
    reasoning: str


# Forced-tool-use schema for the final classification — see
# LLMService.complete_structured. Every field required; decision/severity/
# blast_radius each locked to their real enum, so a malformed or incomplete
# response is impossible at the API level rather than merely discouraged by
# a prompt instruction the model might not follow (it didn't, in production
# — see this module's git history for the regex+fallback parser this
# replaced).
_SUBMIT_TRIAGE_SCHEMA = {
    "name": "submit_triage",
    "description": "Submit the triage classification for this production error event.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["real", "noise", "duplicate"],
                "description": (
                    "real = happening in production, needs investigation. "
                    "noise = transient/expected/false alarm, stand down. "
                    "duplicate = an open PR already covers this — only valid "
                    "if the DUPLICATE CHECK context says DUPLICATE."
                ),
            },
            "severity": {
                "type": "string",
                "enum": ["P0", "P1", "P2", "P3"],
                "description": (
                    "P0 = service down/data loss/blocking all users. "
                    "P1 = degraded/urgent, many users or >100/24h. "
                    "P2 = silent recurring failure, <100/24h. "
                    "P3 = rare or very low impact, <5/24h."
                ),
            },
            "blast_radius": {
                "type": "string",
                "enum": ["single_service", "multi_service", "unknown"],
            },
            "occurrences_24h": {
                "type": "integer",
                "description": "The count from the OCCURRENCE COUNT context above, or 0 if not checked.",
            },
            "duplicate_pr": {
                "type": ["string", "null"],
                "description": "The PR URL from the DUPLICATE CHECK context if decision=duplicate, else null.",
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence explaining the decision.",
            },
        },
        "required": [
            "decision", "severity", "blast_radius",
            "occurrences_24h", "duplicate_pr", "reasoning",
        ],
    },
}


# ---------------------------------------------------------------------------
# TriageAgent
# ---------------------------------------------------------------------------

class TriageAgent(BaseAgent):
    """
    Classifies ErrorEvents as real / noise / duplicate and assigns P0–P3 severity.

    Uses Claude Haiku for fast, cheap classification on every alert.

    Usage (standalone — call before wiring into the incident loop):
        agent = TriageAgent()
        result = await agent.triage(error_event)
        print(result.decision, result.severity, result.occurrences_24h)
    """

    def __init__(
        self,
        aws: AWSService | None = None,
        store=None,
        llm: LLMService | None = None,
    ) -> None:
        # temperature=0.0: this classification has one right answer given the
        # facts (decision + severity, not open-ended reasoning), and running
        # the regression eval found real sample-to-sample drift at the
        # default temperature -- 15/108 held-out cases moved off their
        # original label on a pure replay, zero code changes. Pinning this
        # is the actual fix; loosening the regression gate's pass bar
        # instead would just be tolerating noise, not removing it.
        super().__init__(llm=llm or LLMService(model=HAIKU_MODEL, temperature=0.0))
        self._aws = aws or AWSService()
        self._store = store or _default_store
        self._register_tools()

    def _register_tools(self) -> None:
        aws = self._aws
        store = self._store
        # The target app's logs live in us-east-2, agent-platform's own infra in
        # us-east-1 -- search_log_events() had no region param at all until this
        # fix, so this call always queried the wrong region and failed. See
        # AWSService.search_log_events's docstring for the full story.
        log_region = settings.ecs_log_groups_region or None

        async def _check_duplicate_pr(error_type: str, service: str = "", description_prefix: str = "") -> str:
            key = f"{error_type}:{service}:{description_prefix[:100]}"
            pr_url = store.get_pr_for_resource(key) or store.get_pr_for_resource(error_type)
            if pr_url:
                return f"DUPLICATE: Open PR already exists for '{key}': {pr_url}"
            return f"NO_DUPLICATE: No existing PR found for '{key}'"

        async def _get_occurrence_count(
            log_group: str,
            pattern: str,
            hours: int = 24,
        ) -> str:
            try:
                events = aws.search_log_events(
                    log_group=log_group,
                    filter_pattern=pattern,
                    minutes=hours * 60,
                    limit=1000,
                    region=log_region,
                )
                return (
                    f"{len(events)} occurrences of '{pattern}' "
                    f"in {log_group} over the last {hours}h."
                )
            except AWSError as exc:
                return f"Could not fetch CloudWatch logs: {exc}. Treat occurrence count as unknown."

        # Still registered via register_tool()/self._tools even though
        # triage() below calls these closures directly rather than through
        # the ReAct loop — keeps them independently testable via the same
        # registry every other agent's tools use (see
        # tests/test_search_log_events_region.py) and keeps the option open
        # to route them through self.run() again later if this agent ever
        # needs open-ended exploration instead of two fixed lookups.
        self.register_tool(
            "check_duplicate_pr",
            _check_duplicate_pr,
            (
                "Check if an open PR already exists for this specific error. "
                "Returns DUPLICATE (with PR URL) or NO_DUPLICATE. "
                "Input: {error_type: string, service: string, description_prefix: string (first 100 chars of description)}"
            ),
        )
        self.register_tool(
            "get_occurrence_count",
            _get_occurrence_count,
            (
                "Count how many times an error pattern appeared in CloudWatch logs over the last N hours. "
                "Use hours=24 for blast radius / severity assessment. "
                "Input: {log_group: string, pattern: string, hours: integer (default 24)}"
            ),
        )

    async def triage(self, event: ErrorEvent) -> TriageResult:
        """Run triage on an ErrorEvent. Returns a TriageResult."""
        from app.services.agent_tracker import agent_tracker

        run_id = agent_tracker.start("TriageAgent", incident_id=incident_id_ctx.get())
        try:
            log_group = event.metadata.get("log_group", "")
            pattern = event.metadata.get("pattern", event.error_type or event.title)
            error_type = event.error_type or event.title

            # event.title/description are raw, externally-sourced text (CloudWatch
            # log/alarm messages) — the same content class DiagnosisAgent already
            # scans/wraps as "cloudwatch-logs". Detection-only here, deliberately
            # NOT wrapped: wrap_untrusted's multi-line block measurably
            # destabilized this specific Haiku classification prompt in the real
            # 80-case regression gate. TriageAgent's prompt is small and tightly
            # tuned; the structural-quoting defense's cost outweighs its benefit
            # here specifically -- scan_for_injection still gives real visibility
            # without touching the prompt content at all.
            scan_for_injection(event.title or "", source="cloudwatch-logs")
            scan_for_injection(event.description or "", source="cloudwatch-logs")

            # Deterministic context gathering -- these two calls need no model
            # judgment to decide their arguments (fully derived from `event`),
            # so call the registered closures directly instead of spending a
            # ReAct turn having the model copy these same values into an
            # Action Input.
            check_duplicate_pr_fn, _ = self._tools["check_duplicate_pr"]
            duplicate_check = await check_duplicate_pr_fn(
                error_type=error_type, service=event.service,
                description_prefix=(event.description or "")[:100],
            )
            agent_tracker.increment_tool_call(run_id)

            occurrence_info = "Not checked — no log_group available. Treat occurrences_24h as 0."
            if log_group:
                get_occurrence_count_fn, _ = self._tools["get_occurrence_count"]
                occurrence_info = await get_occurrence_count_fn(
                    log_group=log_group, pattern=pattern, hours=24,
                )
                agent_tracker.increment_tool_call(run_id)

            today = date.today().isoformat()

            prompt = f"""You are a triage agent. Classify this production error event.

TODAY'S DATE: {today}  ← use this as the reference for "recent" / "current" / "future"

ERROR EVENT:
  id          : {event.id}
  source      : {event.source}
  error_type  : {error_type}
  title       : {event.title}
  description : {event.description}
  service     : {event.service}
  log_group   : {log_group or '(not provided)'}
  task_id     : {event.task_id or '(not provided)'}
  detected_at : {event.detected_at.isoformat()}

DUPLICATE CHECK: {duplicate_check}
OCCURRENCE COUNT (24h): {occurrence_info}

SEVERITY GUIDE:
  P0 — service down / data loss / blocking all users
  P1 — degraded / urgent, affecting many users or high frequency (>100/24h)
  P2 — silent recurring failure, needs a fix but not urgent (<100/24h)
  P3 — rare or very low impact (<5/24h)

DECISION GUIDE:
  "real"      — happening in production, needs investigation
  "noise"     — transient / expected / false alarm — stand down
  "duplicate" — open PR already covers this — link and stand down

CRITICAL: You may ONLY choose decision="duplicate" if DUPLICATE CHECK above starts with "DUPLICATE:".
If it says "NO_DUPLICATE", you MUST NOT choose "duplicate" even if you believe a fix is in progress.
Use "real" if the error is happening and no open PR was found.

Call submit_triage with your classification."""

            triage_result = await self._submit_triage(prompt)

            # Structural backstop for the CRITICAL rule above: the schema's
            # enum allows "duplicate" as a value regardless of what DUPLICATE
            # CHECK actually said (an enum can't cross-validate against other
            # fields), so enforce it in code instead of trusting the model
            # followed the prompt instruction -- the same "don't just ask
            # nicely" principle that motivated forcing the tool call at all.
            if triage_result.decision == "duplicate" and not duplicate_check.startswith("DUPLICATE:"):
                logger.warning(
                    "[Triage] event %s: model chose decision=duplicate but check_duplicate_pr "
                    "returned NO_DUPLICATE — overriding to 'real'.", event.id,
                )
                triage_result.decision = "real"
                triage_result.duplicate_pr = None

            # Leaked-marker check only — TriageResult has no citations to check
            # provenance on, and no safe way to force a corrected decision/severity
            # here (unlike MergeDecisionAgent, which can fail-safe to refix_first).
            # Log loudly so a leaked marker is at least visible, matching this
            # check's role everywhere it's detection-only.
            leak_failures = check_leaked_markers(triage_result.reasoning)
            if leak_failures:
                logger.warning(
                    "[Triage] Output validation failed for event %s — %s",
                    event.id, "; ".join(leak_failures),
                )

            agent_tracker.complete(
                run_id, self._llm.last_input_tokens, self._llm.last_output_tokens,
                getattr(self._llm, "_model", "unknown"),
            )
            return triage_result
        except Exception as exc:
            agent_tracker.fail(run_id, str(exc))
            raise

    async def _submit_triage(self, prompt: str) -> TriageResult:
        """Forced tool-use completion — see _SUBMIT_TRIAGE_SCHEMA and
        LLMService.complete_structured for why this replaced regex+json.loads.
        """
        try:
            data = await self._llm.complete_structured(
                messages=[{"role": "user", "content": prompt}],
                tool_schema=_SUBMIT_TRIAGE_SCHEMA,
            )
        except Exception as exc:
            # Still a real, worth-having last-resort fallback (API outage,
            # a truncated response) -- but now a genuine edge case, not the
            # primary defense against the model's own formatting habits.
            logger.error("[Triage] complete_structured failed — defaulting to real/P2: %s", exc)
            return TriageResult(
                decision="real", severity="P2", blast_radius="unknown",
                occurrences_24h=0, duplicate_pr=None,
                reasoning=f"LLM call failed — defaulting to real/P2: {exc}",
            )
        return TriageResult(
            decision=data.get("decision", "real"),
            severity=data.get("severity", "P2"),
            blast_radius=data.get("blast_radius", "unknown"),
            occurrences_24h=int(data.get("occurrences_24h") or 0),
            duplicate_pr=data.get("duplicate_pr"),
            reasoning=data.get("reasoning", ""),
        )
