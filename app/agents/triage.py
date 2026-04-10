"""
TriageAgent — classifies ErrorEvents as real / noise / duplicate and assigns P0–P3 severity.

Model: Claude Haiku (fast, cheap — runs on every alert)

Flow:
  1. check_duplicate_pr   → is there already an open PR for this error type?
  2. get_occurrence_count → how many times has this fired in the last 24 h?
  3. Answer: JSON triage decision

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

import json
import logging
import re
from dataclasses import dataclass

from app.agents.base import BaseAgent
from app.models.events import ErrorEvent
from app.services.aws import AWSError, AWSService
from app.services.incident_store import incident_store as _default_store
from app.services.llm import HAIKU_MODEL, LLMService

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


def _parse_triage_result(answer: str) -> TriageResult:
    """Extract the JSON triage decision from the agent's Answer field."""
    match = re.search(r"\{.*\}", answer, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            return TriageResult(
                decision=data.get("decision", "real"),
                severity=data.get("severity", "P2"),
                blast_radius=data.get("blast_radius", "unknown"),
                occurrences_24h=int(data.get("occurrences_24h", 0)),
                duplicate_pr=data.get("duplicate_pr"),
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    logger.warning("TriageAgent returned non-JSON answer — defaulting to real/P2. Raw: %s", answer[:300])
    return TriageResult(
        decision="real",
        severity="P2",
        blast_radius="unknown",
        occurrences_24h=0,
        duplicate_pr=None,
        reasoning=f"Parse failed — defaulting to real/P2. Raw answer: {answer[:200]}",
    )


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
    ) -> None:
        super().__init__(llm=LLMService(model=HAIKU_MODEL))
        self._aws = aws or AWSService()
        self._store = store or _default_store
        self._register_tools()

    def _register_tools(self) -> None:
        aws = self._aws
        store = self._store

        async def _check_duplicate_pr(error_type: str) -> str:
            pr_url = store.get_pr_for_resource(error_type)
            if pr_url:
                return f"DUPLICATE: Open PR already exists for '{error_type}': {pr_url}"
            return f"NO_DUPLICATE: No existing PR found for '{error_type}'"

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
                )
                return (
                    f"{len(events)} occurrences of '{pattern}' "
                    f"in {log_group} over the last {hours}h."
                )
            except AWSError as exc:
                return f"Could not fetch CloudWatch logs: {exc}. Treat occurrence count as unknown."

        self.register_tool(
            "check_duplicate_pr",
            _check_duplicate_pr,
            (
                "Check if an open PR already exists for this error type. "
                "Returns DUPLICATE (with PR URL) or NO_DUPLICATE. "
                "Input: {error_type: string}"
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
        log_group = event.metadata.get("log_group", "")
        pattern = event.metadata.get("pattern", event.error_type or event.title)
        error_type = event.error_type or event.title

        occurrence_instruction = (
            f'2. Call get_occurrence_count with log_group="{log_group}", '
            f'pattern="{pattern}", hours=24'
            if log_group
            else "2. Skip get_occurrence_count — no log_group available"
        )

        prompt = f"""You are a triage agent. Classify this production error event.

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

STEPS:
1. Call check_duplicate_pr with error_type="{error_type}"
{occurrence_instruction}
3. Output your triage decision as JSON in the Answer field

SEVERITY GUIDE:
  P0 — service down / data loss / blocking all users
  P1 — degraded / urgent, affecting many users or high frequency (>100/24h)
  P2 — silent recurring failure, needs a fix but not urgent (<100/24h)
  P3 — rare or very low impact (<5/24h)

DECISION GUIDE:
  "real"      — happening in production, needs investigation
  "noise"     — transient / expected / false alarm — stand down
  "duplicate" — open PR already covers this — link and stand down

Answer with ONLY a valid JSON object, no other text:
{{
  "decision": "real",
  "severity": "P2",
  "blast_radius": "single_service",
  "occurrences_24h": 47,
  "duplicate_pr": null,
  "reasoning": "one sentence explanation"
}}"""

        result = await self.run(prompt)
        return _parse_triage_result(result.answer)
