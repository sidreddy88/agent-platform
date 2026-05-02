"""
DiagnosisAgent — pulls log context, searches the codebase via RAG, and produces
a root cause analysis with a confidence score.

Model: Claude Sonnet

Flow:
  1. get_error_samples      → recent error occurrences with full messages
  2. check_still_occurring  → is the error happening right now (reproduction check)
  3. get_occurrence_timeline → per-hour breakdown (is it getting worse?)
  4. search_similar_incidents → past incidents with matching symptoms
  5. search_codebase        → RAG search to find candidate file paths
  6. get_file_contents      → fetch the FULL source of the candidate file(s)
                              (RAG returns fragments; full file needed to audit all return paths)
  7. Answer: structured JSON diagnosis

Confidence gate (CONFIDENCE_THRESHOLD = 0.70):
  ≥ 0.70 → status = FIXING (proceed to Fix Generation Agent — Week 3)
  < 0.70 → status = AWAITING_APPROVAL (human escalation via Slack)
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field

from app.agents.base import BaseAgent
from app.core.config import settings
from app.models.events import IncidentState
from app.services.aws import AWSError, AWSService
from app.services.github import GitHubService
from app.services.llm import LLMService
from app.services.rag import RAGService

logger = logging.getLogger(__name__)

CONFIDENCE_THRESHOLD = 0.70

# ---------------------------------------------------------------------------
# Known incidents knowledge base
# ---------------------------------------------------------------------------

_KNOWN_INCIDENTS: list[dict] = [
    {
        "id": "INC-001",
        "symptoms": ["tasks failing", "OOMKilled", "exit code 137", "memory"],
        "root_cause": "Container memory limit too low for workload spike",
        "resolution": "Increased task memory from 512MB to 1024MB and redeployed",
    },
    {
        "id": "INC-002",
        "symptoms": ["500 errors", "error spike", "deployment", "null pointer"],
        "root_cause": "New deployment introduced null reference bug in payment service",
        "resolution": "Rolled back deployment within 8 minutes",
    },
    {
        "id": "INC-003",
        "symptoms": ["connection pool", "timeout", "database", "too many connections"],
        "root_cause": "Connection leak introduced in ORM query refactor",
        "resolution": "Deployed hotfix to close connections, restarted service",
    },
    {
        "id": "INC-004",
        "symptoms": ["high cpu", "slow response", "latency", "timeout", "p99"],
        "root_cause": "Inefficient query missing index, triggered by traffic spike",
        "resolution": "Added database index, CPU normalized within 5 minutes",
    },
    {
        "id": "INC-005",
        "symptoms": ["upstream", "dependency", "connection refused", "circuit breaker", "503"],
        "root_cause": "Third-party API rate limit hit due to retry storm",
        "resolution": "Enabled circuit breaker, added exponential backoff",
    },
    {
        "id": "INC-006",
        "symptoms": ["NoSuchKey", "S3", "key does not exist", "move", "delete", "file"],
        "root_cause": "S3 operation attempted on a key that no longer exists — no existence check before copy/delete",
        "resolution": "Added headObject check before copyObject, or catch NoSuchKey specifically and return early",
    },
    {
        "id": "INC-007",
        "symptoms": ["TypeError", "undefined", "cannot read property", "null", "classification", "llm"],
        "root_cause": "LLM wrapper function does not handle API failure — returns without the expected .classification field when OpenAI call throws, so callers receive an incomplete object",
        "resolution": "Fixed the LLM wrapper to catch errors and return a safe default object with all required fields, so callers always receive a well-formed response",
    },
    {
        "id": "INC-008",
        "symptoms": ["TypeError", "undefined", "cannot read property", "publish_decision", "classification", "wrapper", "intermediate"],
        "root_cause": "Intermediate wrapper function (e.g. runValidationCheck) already guards the underlying API failure but its error/early-exit return paths omit the `classification` field that callers always access — the direct producer of the crashing object is the wrapper, not the deep API call",
        "resolution": "Added `classification: { publish_decision: 'block', ... }` to every return path in the wrapper that previously omitted it, so all callers always receive a complete object regardless of which code path executed",
    },
]


def _find_similar_incidents(symptoms: str) -> list[dict]:
    words = set(symptoms.lower().split())
    scored = []
    for inc in _KNOWN_INCIDENTS:
        inc_words = set(" ".join(inc["symptoms"]).lower().split())
        overlap = len(words & inc_words)
        if overlap > 0:
            scored.append((overlap, inc))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [inc for _, inc in scored[:3]]


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class DiagnosisResult:
    root_cause: str
    confidence: float           # 0.0 – 1.0
    evidence: list[str] = field(default_factory=list)
    fix_approach: str = ""
    affected_function: str | None = None
    affected_file: str | None = None
    reproduction_confirmed: bool = False
    escalate: bool = False      # True when confidence < CONFIDENCE_THRESHOLD
    raw_llm: str = ""


def _parse_diagnosis_result(answer: str) -> DiagnosisResult:
    # Extraction strategies in priority order:
    # 1. JSON fenced code block  (```json ... ```)
    # 2. Last {...} block        (LLMs sometimes emit a preamble with stray braces)
    # 3. First {...} block       (original behaviour)
    candidates: list[str] = []
    code_block = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", answer, re.DOTALL)
    if code_block:
        candidates.append(code_block.group(1))
    all_blocks = re.findall(r"\{.*?\}", answer, re.DOTALL)
    if all_blocks:
        candidates.append(all_blocks[-1])   # last block
        if len(all_blocks) > 1:
            candidates.append(all_blocks[0])  # first block as last resort

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if not isinstance(data, dict):
                continue
            confidence = float(data.get("confidence", 0.5))
            return DiagnosisResult(
                root_cause=data.get("root_cause", "Unknown"),
                confidence=confidence,
                evidence=data.get("evidence", []),
                fix_approach=data.get("fix_approach", ""),
                affected_function=data.get("affected_function"),
                affected_file=data.get("affected_file"),
                reproduction_confirmed=bool(data.get("reproduction_confirmed", False)),
                escalate=confidence < CONFIDENCE_THRESHOLD,
                raw_llm=answer,
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

    logger.warning("DiagnosisAgent returned non-JSON answer — using low-confidence fallback. Raw: %.200s", answer)
    return DiagnosisResult(
        root_cause="Could not parse diagnosis — manual review required",
        confidence=0.0,
        escalate=True,
        raw_llm=answer,
    )


# ---------------------------------------------------------------------------
# DiagnosisAgent
# ---------------------------------------------------------------------------

class DiagnosisAgent(BaseAgent):
    """
    Produces a root cause analysis with confidence score for a triaged incident.

    Uses Claude Sonnet for deeper reasoning than the TriageAgent.

    Usage:
        agent = DiagnosisAgent()
        result = await agent.diagnose(incident)
        if result.escalate:
            # notify human — confidence below threshold
        else:
            # proceed to Fix Generation Agent (Week 3)
    """

    def __init__(
        self,
        aws: AWSService | None = None,
        rag: RAGService | None = None,
        github: GitHubService | None = None,
    ) -> None:
        super().__init__(llm=LLMService())   # Sonnet — default model
        self._aws = aws or AWSService()
        self._rag = rag
        self._github = github or GitHubService()
        _owner, _repo = settings.fix_target_repo.split("/", 1)
        self._owner = _owner
        self._repo = _repo
        self._register_tools()

    def _register_tools(self) -> None:
        aws = self._aws
        rag = self._rag
        github = self._github
        owner = self._owner
        repo = self._repo

        async def _get_error_samples(
            log_group: str,
            pattern: str,
            minutes: int = 60,
            limit: int = 10,
        ) -> str:
            """Get the most recent error occurrences with full log messages."""
            try:
                events = aws.search_log_events(
                    log_group=log_group,
                    filter_pattern=pattern,
                    minutes=minutes,
                    limit=limit,
                )
                if not events:
                    return f"No '{pattern}' events in the last {minutes} min."
                lines = [f"[{e['timestamp']}] stream={e['stream'].rsplit('/', 1)[-1][:8]}... {e['message'][:200]}"
                         for e in events]
                return f"{len(events)} sample(s):\n" + "\n".join(lines)
            except AWSError as exc:
                return f"Could not fetch logs: {exc}"

        async def _check_still_occurring(log_group: str, pattern: str) -> str:
            """Check if the error is still occurring in the last 10 minutes (reproduction check)."""
            try:
                events = aws.search_log_events(
                    log_group=log_group,
                    filter_pattern=pattern,
                    minutes=10,
                    limit=5,
                )
                if events:
                    return f"CONFIRMED: {len(events)} occurrence(s) in the last 10 min. Error is ongoing."
                return "NOT_CONFIRMED: No occurrences in the last 10 min. Error may have stopped."
            except AWSError as exc:
                return f"Could not check: {exc}"

        async def _get_occurrence_timeline(
            log_group: str,
            pattern: str,
            hours: int = 24,
        ) -> str:
            """Get per-hour occurrence count to understand if the error is accelerating."""
            try:
                events = aws.search_log_events(
                    log_group=log_group,
                    filter_pattern=pattern,
                    minutes=hours * 60,
                    limit=1000,
                )
                if not events:
                    return f"No '{pattern}' events in the last {hours}h."
                hourly: Counter = Counter()
                for ev in events:
                    hour = ev["timestamp"][:13]   # "2026-04-10T10"
                    hourly[hour] += 1
                lines = [f"  {h}: {c}" for h, c in sorted(hourly.items())]
                return (
                    f"Occurrence timeline (last {hours}h) — {len(events)} total:\n"
                    + "\n".join(lines)
                )
            except AWSError as exc:
                return f"Could not fetch timeline: {exc}"

        async def _search_codebase(query: str) -> str:
            """Search the indexed codebase for code relevant to the incident."""
            if rag is None:
                return "RAG not configured — codebase search unavailable."
            try:
                chunks = await rag.search(query, n_results=4)
            except Exception as exc:
                return f"RAG search error: {exc}"
            if not chunks:
                return "No relevant code found in indexed codebase."
            parts = []
            for c in chunks:
                parts.append(
                    f"--- {c.file_path}:{c.start_line}-{c.end_line} (score={c.score}) ---\n"
                    f"{c.content[:400]}"
                )
            return "\n\n".join(parts)

        async def _get_file_contents(file_path: str) -> str:
            """Fetch the full source of a file from the target repo. Use this to read ALL return paths of a function — RAG only returns fragments."""
            try:
                content, _ = await github.get_file_contents(owner, repo, file_path.lstrip("/"))
                # Cap at 8 000 chars to stay within context — enough for any single file
                if len(content) > 8000:
                    return content[:8000] + f"\n\n[truncated — {len(content)} chars total]"
                return content
            except Exception as exc:
                return f"Could not fetch {file_path}: {exc}"

        async def _search_similar_incidents(symptoms: str) -> str:
            """Find past incidents with similar symptoms from the knowledge base."""
            matches = _find_similar_incidents(symptoms)
            if not matches:
                return "No similar past incidents found."
            lines = ["Similar past incidents:"]
            for inc in matches:
                lines.append(
                    f"\n  {inc['id']}:\n"
                    f"    Root cause : {inc['root_cause']}\n"
                    f"    Resolution : {inc['resolution']}"
                )
            return "\n".join(lines)

        self.register_tool(
            "get_error_samples",
            _get_error_samples,
            (
                "Get recent error occurrences with full log messages. "
                "Input: {log_group: string, pattern: string, minutes: int (default 60), limit: int (default 10)}"
            ),
        )
        self.register_tool(
            "check_still_occurring",
            _check_still_occurring,
            (
                "Check if the error is still happening in the last 10 minutes. "
                "Use this for reproduction confirmation. "
                "Input: {log_group: string, pattern: string}"
            ),
        )
        self.register_tool(
            "get_occurrence_timeline",
            _get_occurrence_timeline,
            (
                "Get per-hour occurrence counts over the last N hours. "
                "Tells you if the error is accelerating or stable. "
                "Input: {log_group: string, pattern: string, hours: int (default 24)}"
            ),
        )
        self.register_tool(
            "search_codebase",
            _search_codebase,
            (
                "Search the indexed codebase for code relevant to the incident. "
                "Use function names, error types, or file paths as the query. "
                "Input: {query: string}"
            ),
        )
        self.register_tool(
            "get_file_contents",
            _get_file_contents,
            (
                "Fetch the complete source of a file from the target repository. "
                "Use this after search_codebase identifies a candidate file — read the FULL file "
                "to see every return statement and code path, not just RAG fragments. "
                "Critical for null/undefined errors: you must read all return paths of the "
                "producer function to find which one omits the expected field. "
                "Input: {file_path: string (e.g. 'constants/validationMain.js')}"
            ),
        )
        self.register_tool(
            "search_similar_incidents",
            _search_similar_incidents,
            (
                "Search the incident knowledge base for past incidents with similar symptoms. "
                "Input: {symptoms: string (space-separated keywords)}"
            ),
        )

    async def diagnose(self, incident: IncidentState, prior_context: str | None = None) -> DiagnosisResult:
        """Run diagnosis on a triaged incident. Returns a DiagnosisResult."""
        event = incident.error_event
        log_group = event.metadata.get("log_group", "")
        pattern = event.metadata.get("pattern", event.error_type or event.title)

        prior_section = ""
        if prior_context:
            prior_section = f"\nPRIOR KNOWLEDGE (from past incidents — treat as strong evidence):\n{prior_context}\n"

        prompt = f"""You are a senior SRE diagnosing a production incident. A triage agent has already
confirmed this is real. Your job is to find the root cause and a fix approach.

INCIDENT:
  error_type      : {event.error_type}
  title           : {event.title}
  description     : {event.description}
  service         : {event.service}
  log_group       : {log_group or '(not provided)'}
  pattern         : {pattern}
  task_id         : {event.task_id or '(not provided)'}
  severity        : {event.severity}
  occurrences_24h : {incident.occurrences_24h}
  blast_radius    : {incident.blast_radius}
  triage_reasoning: {incident.triage_reasoning}
{prior_section}
STEPS — call tools in this order:
1. get_error_samples — see the actual error messages (log_group="{log_group}", pattern="{pattern}", minutes=120)
2. check_still_occurring — confirm if error is ongoing (log_group="{log_group}", pattern="{pattern}")
3. get_occurrence_timeline — understand the trend (log_group="{log_group}", pattern="{pattern}", hours=24)
4. search_similar_incidents — check knowledge base (symptoms="{event.error_type or ''} {event.description[:50]}")
5. search_codebase — find candidate file paths (query="{event.error_type or event.title}")
6. get_file_contents — fetch the FULL source of the file(s) identified in step 5.
   RAG returns 400-char fragments — you MUST read the full file to see every return statement.
   For null/undefined errors: find the function that returns the crashing object and read
   ALL its return paths to find which one omits the expected field.
7. Answer with a JSON diagnosis

CRITICAL — NULL / UNDEFINED ERRORS:
If the error is a TypeError (cannot read property, undefined, null) or NullPointerException:

STEP 1 — identify the DIRECT producer of the crashing object:
  The crash is `obj.field` or `obj.field.subfield`. Find the function whose RETURN VALUE
  is assigned to `obj` at the crash site. That is the direct producer.
  - It may be a wrapper/intermediate function (e.g. runValidationCheck), NOT the deep API call.
  - The direct producer may already handle errors internally — but its error return paths
    may omit the field callers expect. That IS the root cause.

STEP 2 — check ALL return paths of that producer:
  Read every `return` statement. Does every path include the field the caller accesses?
  BAD pattern: happy path returns {{ ok: true, classification: {{...}} }}
               error path returns {{ ok: false, error: "..." }}   ← missing `classification`
  The missing field on the error path is the root cause, not the bottom-level API failure.

STEP 3 — write root_cause and fix_approach:
  root_cause MUST name the DIRECT PRODUCER function and which return path omits the field.
  BAD:  "classifyResult.classification is undefined when accessed at line 296"
  BAD:  "classifyFields() returns null when OpenAI fails"   ← wrong level if wrapper already handles it
  GOOD: "runValidationCheck() returns {{ ok: false, error: '...' }} on classification failure but omits
         the `classification` field — callers always access .classification so all error return paths
         must include it"

  fix_approach MUST add the missing field to the error return path(s) of the direct producer.
  Do NOT suggest null checks, optional chaining (?.), nullish coalescing (??), or try/catch
  at the property access site — those are symptom fixes that hide the problem.
  BAD:  "Add defensive validation: check if classifyResult?.classification exists before accessing"
  GOOD: "In runValidationCheck(), add `classification: {{ publish_decision: 'block', ... }}` to every
         return path that currently omits it (error returns, early exits, catch blocks)"

- affected_function should be the DIRECT PRODUCER function, not the crash function or the deep API.

Answer with ONLY a valid JSON object:
{{
  "root_cause": "precise description of WHY the error occurs — name the producer function for null errors",
  "confidence": 0.82,
  "evidence": [
    "specific fact from logs or code that supports root cause",
    "another concrete observation"
  ],
  "fix_approach": "what the PRODUCER function must do differently — never 'add null check at access site'",
  "affected_function": "functionName or null",
  "affected_file": "path/to/file.js or null",
  "reproduction_confirmed": true
}}

Confidence guide:
  0.90+ → near certain, clear evidence in code + logs
  0.70-0.90 → probable, strong log evidence but limited code visibility
  0.50-0.70 → possible, pattern matches but incomplete evidence
  <0.50 → uncertain, escalate to human"""

        result = await self.run(prompt)
        return _parse_diagnosis_result(result.answer)
