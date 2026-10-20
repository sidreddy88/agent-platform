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

The decision is a forced tool call (LLMService.complete_structured, tool_choice
locked to submit_merge_decision) rather than JSON-in-prose extracted with a
regex: decision is guaranteed one of exactly two values, blocking_issues/
non_blocking_issues are guaranteed real arrays, at the API level. This
replaced a regex-extract + json.loads + hardcoded-fallback-default parser
that silently degraded to decision="refix_first" on any malformed response —
conservative, but an undetectable failure mode either way, since nothing
distinguished "the model reasoned to refix_first" from "the parser gave up."
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.models.events import IncidentState
from app.services.ipi_guard import scan_for_injection, wrap_untrusted
from app.services.llm import HAIKU_MODEL, LLMService
from app.services.output_validator import check_leaked_markers

logger = logging.getLogger(__name__)


@dataclass
class MergeDecision:
    decision: str          # "merge_now" | "refix_first"
    reasoning: str
    blocking_issues: list[str]
    non_blocking_issues: list[str]


# Forced-tool-use schema — see LLMService.complete_structured. decision locked
# to its real enum; blocking_issues/non_blocking_issues guaranteed arrays
# (possibly empty) rather than a field the model might omit or return as a
# bare string when it has nothing to report.
_SUBMIT_MERGE_DECISION_SCHEMA = {
    "name": "submit_merge_decision",
    "description": "Submit the merge decision for an AI-generated fix PR that received REQUEST_CHANGES.",
    "input_schema": {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["merge_now", "refix_first"],
                "description": (
                    "merge_now = core fix is correct, remaining issues are non-blocking. "
                    "refix_first = fix may be wrong, introduces risk, or has a blocking issue."
                ),
            },
            "blocking_issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Blocking issues found, empty array if none.",
            },
            "non_blocking_issues": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Non-blocking issues found, empty array if none.",
            },
            "reasoning": {
                "type": "string",
                "description": "One sentence explaining the decision.",
            },
        },
        "required": ["decision", "blocking_issues", "non_blocking_issues", "reasoning"],
    },
}


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

        # event.title is raw CloudWatch text (same class DiagnosisAgent/TriageAgent
        # scan); review_text is CodeReviewAgent's own free-text output, which can
        # itself carry an unwrapped injection forward from an unsanitized PR diff
        # (CodeReviewAgent doesn't guard diff content — see docs/blog-drafts notes).
        # A manipulated review_text could flip merge_now vs refix_first here.
        scan_for_injection(event.title or "", source="cloudwatch-logs")
        scan_for_injection(review_text, source="code-review-output")
        wrapped_review = wrap_untrusted(review_text[:3000], source="code-review-output")

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
{wrapped_review}

CLASSIFICATION RULES:
  BLOCKING issues (always → refix_first):
    - The fix addresses the wrong function or wrong root cause
    - New bugs, crashes, or regressions INTRODUCED BY THIS PR
    - Security vulnerabilities INTRODUCED BY THIS PR (not pre-existing patterns)
    - Syntactically invalid or incomplete code (truncated lines, missing braces)
    - Data loss or corruption risk introduced by this change
    - Fix breaks other existing functionality

  NON-BLOCKING issues (ok to merge_now if severity is P0 or P1):
    - Missing unit tests or test coverage
    - Additional error handling that would be nice to have
    - Logging improvements
    - Code style, naming, or documentation
    - Performance improvements (not regressions)
    - Pre-existing security patterns not introduced by this PR
      (e.g. missing auth on an endpoint that had no auth before this change)
    - Prototype pollution / input validation for edge cases that aren't in the hot path

DECISION LOGIC:
  - Any BLOCKING issue present → refix_first (regardless of severity)
  - P0 or P1 + only NON-BLOCKING issues → merge_now (get the fix in, follow up later)
  - P2 or P3 + only NON-BLOCKING issues → refix_first (there's time to do it properly)

IMPORTANT: If the review flags a security or quality issue that existed in the codebase
BEFORE this PR, that is non-blocking. This PR is judged only on what it changes.

Call submit_merge_decision with your decision."""

        try:
            data = await self._llm.complete_structured(
                messages=[{"role": "user", "content": prompt}],
                tool_schema=_SUBMIT_MERGE_DECISION_SCHEMA,
                system=(
                    "You are a senior engineering manager deciding whether a critical production fix "
                    "should be merged immediately or sent back for improvements. Be precise about "
                    "what is blocking vs non-blocking."
                ),
            )
            result = MergeDecision(
                decision=data.get("decision", "refix_first"),
                reasoning=data.get("reasoning", ""),
                blocking_issues=data.get("blocking_issues", []),
                non_blocking_issues=data.get("non_blocking_issues", []),
            )

            # Leaked-marker check only. Unlike TriageAgent, this agent has a
            # real fail-safe available: if the reasoning echoes an ipi_guard
            # marker, don't trust the merge_now/refix_first verdict it's
            # attached to — force the conservative branch. review_text (the
            # most likely source of a leaked marker, since it's the untrusted
            # content wrapped above) doesn't leak into `result`, so scanning
            # `result.reasoning` is the right surface: it's the model's own
            # words, and an echoed marker there means it copied wrapped
            # content into its answer instead of reasoning about it.
            leak_failures = check_leaked_markers(
                result.reasoning, *result.blocking_issues, *result.non_blocking_issues,
            )
            if leak_failures:
                logger.warning(
                    "[MergeDecision] Output validation failed for %s — %s — forcing refix_first",
                    incident.id, "; ".join(leak_failures),
                )
                result.decision = "refix_first"
                result.reasoning = (
                    f"OUTPUT VALIDATION WARNING — forced refix_first: {'; '.join(leak_failures)}"
                )

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
