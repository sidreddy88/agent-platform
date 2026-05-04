"""
MergeDecisionAgent — decides whether to merge a PR despite REQUEST_CHANGES from code review.

Called only when code review returns REQUEST_CHANGES. The agent weighs:
  1. Issue severity (P0/P1 = critical, P2/P3 = routine)
  2. Occurrence count (how often is this hitting production?)
  3. Whether outstanding review issues are BLOCKING or NON-BLOCKING

Decision:
  "merge_now"   — core fix is correct, remaining issues are non-blocking
                  (missing tests, additional error handling, style improvements).
                  For critical incidents this is the right call — ship the fix now,
                  follow up with improvements in a separate PR.
  "refix_first" — fix may be wrong, introduces risk, or has blocking issues
                  (incorrect logic, security concern, data loss, breaks other functionality).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.models.events import IncidentState
from app.services.llm import HAIKU_MODEL, LLMService

logger = logging.getLogger(__name__)


@dataclass
class MergeDecision:
    decision: str          # "merge_now" | "refix_first"
    reasoning: str
    blocking_issues: list[str]
    non_blocking_issues: list[str]


def _parse(raw: str) -> MergeDecision:
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            d = json.loads(match.group())
            return MergeDecision(
                decision=d.get("decision", "refix_first"),
                reasoning=d.get("reasoning", ""),
                blocking_issues=d.get("blocking_issues", []),
                non_blocking_issues=d.get("non_blocking_issues", []),
            )
        except (json.JSONDecodeError, ValueError):
            pass
    logger.warning("[MergeDecision] Non-JSON response — defaulting to refix_first. Raw: %s", raw[:200])
    return MergeDecision(
        decision="refix_first",
        reasoning=f"Parse failed — defaulting to refix_first. Raw: {raw[:200]}",
        blocking_issues=[],
        non_blocking_issues=[],
    )


class MergeDecisionAgent:
    """
    Single LLM call (Haiku) — no tool use needed, pure classification.

    Usage:
        agent = MergeDecisionAgent()
        result = await agent.decide(incident, review_text)
        if result.decision == "merge_now":
            # proceed to approval gate
    """

    def __init__(self, llm: LLMService | None = None) -> None:
        self._llm = llm or LLMService(model=HAIKU_MODEL)

    async def decide(self, incident: IncidentState, review_text: str) -> MergeDecision:
        event = incident.error_event
        severity = str(event.severity).split(".")[-1] if event.severity else "P2"
        occurrences = incident.occurrences_24h or 0

        prompt = f"""A code review returned REQUEST_CHANGES on an AI-generated fix PR.
Your job: decide whether to merge the PR now (core fix is correct, remaining issues are minor)
or wait for a re-fix (fix is wrong or introduces meaningful risk).

INCIDENT:
  Severity    : {severity}
  Occurrences : {occurrences} in last 24h
  Title       : {event.title}
  Root cause  : {incident.diagnosis or '(not available)'}
  Fix file    : {incident.diagnosis_affected_file or '(unknown)'}

CODE REVIEW (the review that returned REQUEST_CHANGES):
{review_text[:3000]}

CLASSIFICATION RULES:
  BLOCKING issues (always → refix_first):
    - The fix addresses the wrong function or wrong root cause
    - New bugs, crashes, or regressions introduced by the fix
    - Security vulnerabilities (SQL injection, auth bypass, data exposure)
    - Data loss or corruption risk
    - Fix breaks other existing functionality

  NON-BLOCKING issues (ok to merge_now if severity is P0 or P1):
    - Missing unit tests or test coverage
    - Additional error handling that would be nice to have
    - Logging improvements
    - Code style, naming, or documentation
    - Performance improvements (not regressions)
    - Prototype pollution / input validation for edge cases that aren't in the hot path

DECISION LOGIC:
  - Any BLOCKING issue present → refix_first (regardless of severity)
  - P0 or P1 + only NON-BLOCKING issues → merge_now (get the fix in, follow up later)
  - P2 or P3 + only NON-BLOCKING issues → refix_first (there's time to do it properly)

Respond with ONLY a valid JSON object:
{{
  "decision": "merge_now" | "refix_first",
  "blocking_issues": ["<list of blocking issues found, empty if none>"],
  "non_blocking_issues": ["<list of non-blocking issues found>"],
  "reasoning": "<one sentence explaining the decision>"
}}"""

        try:
            raw = await self._llm.complete(
                messages=[{"role": "user", "content": prompt}],
                system=(
                    "You are a senior engineering manager deciding whether a critical production fix "
                    "should be merged immediately or sent back for improvements. Be precise about "
                    "what is blocking vs non-blocking. Return only valid JSON."
                ),
            )
            result = _parse(raw)
            logger.info(
                "[MergeDecision] %s — decision=%s (sev=%s, %d occ/24h): %s",
                incident.id, result.decision, severity, occurrences, result.reasoning,
            )
            return result
        except Exception as exc:
            logger.error("[MergeDecision] LLM call failed for %s: %s", incident.id, exc)
            return MergeDecision(
                decision="refix_first",
                reasoning=f"LLM error — defaulting to refix_first: {exc}",
                blocking_issues=[],
                non_blocking_issues=[],
            )
