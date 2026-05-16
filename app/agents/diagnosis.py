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

import asyncio
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
    {
        "id": "INC-009",
        "symptoms": ["duplicate key", "E11000", "insertMany", "bulk write", "parallel", "workers", "race condition", "concurrent", "chunks"],
        "root_cause": "Parallel workers each receive a chunk of the same input; duplicates within the file are not eliminated before chunking, so the same unique key can land in two worker chunks simultaneously — the first worker inserts it, the second hits a duplicate key error",
        "resolution": "Deduplicate the input data by the unique key before splitting into chunks so each value appears in exactly one worker's chunk",
    },
]

_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "is", "are", "was", "were", "be", "been",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "cannot", "can", "not", "no", "in", "on",
    "at", "to", "of", "with", "from", "by", "for", "about",
    "error", "type", "null", "undefined", "read", "property", "object",
})

# ---------------------------------------------------------------------------
# Prose-symbol extraction (used by the grounding guard)
# ---------------------------------------------------------------------------

# Match any camelCase identifier — starts lowercase, has at least one capital
# segment afterwards. Covers all forms the model uses when citing names in
# prose: bare mentions ("callVisionAPI fails"), call sites ("foo()"), and
# backtick-wrapped names. Bare-name matching means common variables like
# `imageBuffer` or `userId` also match — that's fine: if they exist in the
# repo they verify in one Code Search hit; if they're fabricated, the warning
# is justified.
_CAMEL_RE = re.compile(r"\b([a-z][A-Za-z0-9_]*[A-Z][A-Za-z0-9_]*)\b")

# Builtins/methods that match the camelCase shape but should not be verified —
# they almost always show up in some source file by coincidence (false-pass)
# and burning lookups on them adds latency.
_BUILTIN_CAMEL = frozenset({
    "toString", "valueOf", "hasOwnProperty", "isPrototypeOf",
    "propertyIsEnumerable", "toLocaleString",
    "getTime", "getDate", "getMonth", "getFullYear", "getHours",
    "getMinutes", "getSeconds", "getMilliseconds", "getUTCDate",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "toLowerCase", "toUpperCase", "parseInt", "parseFloat",
    "forEach", "indexOf", "lastIndexOf", "isArray", "isFinite", "isNaN",
    "innerHTML", "outerHTML", "appendChild", "removeChild",
    "addEventListener", "removeEventListener",
    "querySelector", "querySelectorAll", "getElementById",
})

# Maximum number of prose candidates we'll verify per diagnosis. Bounds API
# usage; if a diagnosis names more than this many functions in prose, the model
# is probably brainstorming and the whole thing should be reviewed anyway.
_MAX_PROSE_CANDIDATES = 8


def _extract_prose_symbols(text: str) -> list[str]:
    """Pull candidate function-name tokens from prose for grounding verification."""
    if not text:
        return []
    seen: dict[str, None] = {}
    for m in _CAMEL_RE.finditer(text):
        name = m.group(1)
        if len(name) < 4 or name in _BUILTIN_CAMEL:
            continue
        seen.setdefault(name, None)
    return list(seen)


# Names + evidence phrases that indicate a function has no internal callers
# (the entry-point case where empty blast_radius is honest). Used by the
# blast-radius coverage check in `_enforce_grounding`.
_ENTRY_POINT_NAME_HINTS = (
    "handler", "route", "endpoint", "controller", "cron", "job", "worker",
    "task", "main", "lambda", "consumer", "listener",
)
_ENTRY_POINT_EVIDENCE_HINTS = (
    "route handler", "no callers", "entry point", "top-level", "express handler",
    "webhook handler", "cron job", "lambda handler", "fastapi route",
)


def _looks_like_entry_point(fn_name: str, evidence: list[str]) -> bool:
    """Heuristic: does this function look like an external entry point?"""
    name_lower = fn_name.lower()
    if any(hint in name_lower for hint in _ENTRY_POINT_NAME_HINTS):
        return True
    blob = " ".join(evidence or []).lower()
    return any(hint in blob for hint in _ENTRY_POINT_EVIDENCE_HINTS)


def _find_similar_incidents(symptoms: str) -> list[dict]:
    words = {w for w in symptoms.lower().split() if w not in _STOPWORDS}
    scored = []
    for inc in _KNOWN_INCIDENTS:
        inc_words = {w for w in " ".join(inc["symptoms"]).lower().split() if w not in _STOPWORDS}
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
    additional_fix: str | None = None          # secondary change description
    additional_fix_function: str | None = None # secondary function name
    additional_fix_file: str | None = None     # secondary file path
    reproduction_confirmed: bool = False
    escalate: bool = False      # True when confidence < CONFIDENCE_THRESHOLD
    # Pre-fix-reasoning fields. FixGenerationAgent reads these as constraints
    # so its prompt can frame Tier 2 callers as "must not break" and surface
    # contract changes loudly. Empty list / "none" are honest defaults when
    # the diagnosis can't determine the answer — better than fabrication.
    blast_radius: list[dict] = field(default_factory=list)
    # ^ each entry: {"file": str, "function": str, "snippet": str}
    contract_change: str = "none"  # "none" | "signature" | "return_type" | "side_effect"
    contract_change_detail: str | None = None
    raw_llm: str = ""


def _extract_json_object(text: str) -> str | None:
    """Return the first top-level {...} from text, respecting quoted strings and nested braces."""
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_string:
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
    return None


def _parse_diagnosis_result(answer: str) -> DiagnosisResult:
    # Extraction strategies in priority order:
    # 1. JSON fenced code block  (```json ... ```)
    # 2. Unclosed fence  (```json {...   — LLM hit max_tokens or dropped the
    #    closing fence). Without this, a truncation drops us straight to the
    #    placeholder fallback.
    # 3. Balanced brace extraction — handles {} nested inside string values
    candidates: list[str] = []
    code_block = re.search(r"```(?:json)?\s*(.*?)\s*```", answer, re.DOTALL)
    if code_block:
        block = code_block.group(1).strip()
        if block.startswith("{"):
            candidates.append(block)
    else:
        unclosed = re.search(r"```(?:json)?\s*(\{.*)", answer, re.DOTALL)
        if unclosed:
            candidates.append(unclosed.group(1).strip())
    outer = _extract_json_object(answer)
    if outer:
        candidates.append(outer)

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if not isinstance(data, dict):
                continue
            confidence = float(data.get("confidence", 0.5))

            # Normalise blast_radius — accept the structured form from the
            # prompt schema, drop entries that don't have a usable file path.
            raw_br = data.get("blast_radius", []) or []
            blast_radius: list[dict] = []
            if isinstance(raw_br, list):
                for entry in raw_br:
                    if isinstance(entry, dict) and entry.get("file"):
                        blast_radius.append({
                            "file": str(entry["file"]),
                            "function": str(entry.get("function", "") or ""),
                            "snippet": str(entry.get("snippet", "") or "")[:400],
                        })

            contract_change = str(data.get("contract_change", "none") or "none").lower()
            if contract_change not in ("none", "signature", "return_type", "side_effect"):
                contract_change = "none"
            contract_detail = data.get("contract_change_detail")
            if contract_detail is not None and not isinstance(contract_detail, str):
                contract_detail = str(contract_detail)

            return DiagnosisResult(
                root_cause=data.get("root_cause", "Unknown"),
                confidence=confidence,
                evidence=data.get("evidence", []),
                fix_approach=data.get("fix_approach", ""),
                affected_function=data.get("affected_function"),
                affected_file=data.get("affected_file"),
                additional_fix=data.get("additional_fix"),
                additional_fix_function=data.get("additional_fix_function"),
                additional_fix_file=data.get("additional_fix_file"),
                reproduction_confirmed=bool(data.get("reproduction_confirmed", False)),
                escalate=confidence < CONFIDENCE_THRESHOLD,
                blast_radius=blast_radius,
                contract_change=contract_change,
                contract_change_detail=contract_detail,
                raw_llm=answer,
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            continue

    # 200-char truncation made past failures unactionable. 4000 covers the
    # full LLM response in practice (max_tokens is 4096).
    logger.warning("DiagnosisAgent returned non-JSON answer — using low-confidence fallback. Raw: %.4000s", answer)
    return DiagnosisResult(
        root_cause="Could not parse diagnosis — manual review required",
        confidence=0.0,
        escalate=True,
        raw_llm=answer,
    )


# Sentinel for the parse-failure fallback — callers test against this to
# decide whether to retry or accept the result.
_PARSE_FAILURE_ROOT_CAUSE = "Could not parse diagnosis — manual review required"


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
        self._tree_cache: dict[tuple[str, str, str], set[str]] = {}  # (owner, repo, sha) → paths
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
            """Fetch the full source of a file from the target repo."""
            try:
                content, _ = await github.get_file_contents(owner, repo, file_path.lstrip("/"))
                if len(content) > 12000:
                    return (
                        content[:12000]
                        + f"\n\n[TRUNCATED — file is {len(content)} chars, only first 12000 shown. "
                        f"If the function you need is not visible, call get_file_contents again "
                        f"with a more specific path or search for the function name via search_codebase.]"
                    )
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

        async def _verify_symbol_in_repo(symbol: str) -> str:
            """Verify a function/symbol name actually exists in the target repo.

            Uses GitHub Code Search (authoritative for the default branch). Returns
            matching file paths + matched fragments, or NOT_FOUND. Use this BEFORE
            naming a function in affected_function / additional_fix_function so you
            never invent a symbol that doesn't exist.
            """
            name = (symbol or "").strip()
            if not name:
                return "NOT_FOUND: empty symbol."
            # Code Search supports bare-token queries. The trailing '(' nudges toward
            # call/definition sites and away from prose mentions.
            queries = [f'"{name}("', f'"{name}"']
            for q in queries:
                try:
                    hits = await github.search_code(owner, repo, q)
                except Exception as exc:
                    return f"VERIFY_ERROR: {exc}"
                if hits:
                    lines = [f"FOUND ({len(hits)} match(es)) for '{name}':"]
                    for h in hits[:5]:
                        frag = (h.get("fragment") or "").replace("\n", " ").strip()[:160]
                        lines.append(f"  - {h['path']}  :: {frag}")
                    return "\n".join(lines)
            return (
                f"NOT_FOUND: '{name}' does not appear in repo {owner}/{repo} on the default branch. "
                f"DO NOT name this symbol in affected_function or additional_fix_function. "
                f"Set the function field to null, lower confidence to ≤0.65, and surface candidate "
                f"file paths in evidence instead."
            )

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
                "Use specific terms from the stack trace (function names, file names) or "
                "the operation that failed (e.g. 'insertMany appmasterreferrals', "
                "'classifyFields OpenAI') — not just the raw error type. "
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
                "If the file calls workers, helpers, or other modules relevant to the failure, "
                "call this again on those files. Follow the code until you reach the failure site. "
                "If a file is truncated, search for the specific function name via search_codebase. "
                "Input: {file_path: string (e.g. 'constants/validationMain.js')}"
            ),
        )
        self.register_tool(
            "search_similar_incidents",
            _search_similar_incidents,
            (
                "Search the incident knowledge base for past incidents with similar symptoms. "
                "Input: {symptoms: string (space-separated domain-specific keywords — "
                "use operation names, error codes, and system components, not generic words like 'error' or 'null')}"
            ),
        )
        self.register_tool(
            "verify_symbol_in_repo",
            _verify_symbol_in_repo,
            (
                "Verify a function/symbol name actually exists in the target repo (GitHub Code "
                "Search on the default branch). Returns matching file paths or NOT_FOUND. "
                "MANDATORY: call this on every function name BEFORE writing it into "
                "affected_function or additional_fix_function. If NOT_FOUND, the symbol does not "
                "exist — do not name it; null the field and lower confidence. "
                "Input: {symbol: string (e.g. 'processAndStoreImage')}"
            ),
        )

    async def _get_repo_tree(self) -> set[str]:
        """Return the cached file-path set for the target repo, fetching if needed."""
        paths, sha = await self._github.get_file_tree(self._owner, self._repo)
        key = (self._owner, self._repo, sha)
        if key not in self._tree_cache:
            self._tree_cache[key] = paths
        return self._tree_cache[key]

    async def _file_exists_in_repo(self, path: str) -> bool:
        """O(1) membership check against the cached repo tree."""
        p = (path or "").strip().lstrip("/")
        if not p:
            return False
        try:
            tree = await self._get_repo_tree()
            return p in tree
        except Exception:
            return False

    async def _symbol_exists_in_repo(self, symbol: str) -> bool:
        """Check whether `symbol` appears in the target repo on the default branch.

        Authoritative grounding check: the LLM may invent function names that look
        plausible but don't exist. We re-verify post-parse so a fabricated name can't
        leak into the Fix Generation Agent.
        """
        name = (symbol or "").strip()
        if not name:
            return False
        for q in (f'"{name}("', f'"{name}"'):
            try:
                hits = await self._github.search_code(self._owner, self._repo, q)
            except Exception:
                # Treat transient lookup errors as "unknown" — fall through to next query.
                continue
            if hits:
                return True
        return False

    async def _enforce_grounding(self, result: DiagnosisResult) -> DiagnosisResult:
        """Cap confidence and null out function names that don't exist in the repo.

        Catches the case where the LLM violates the prompt's grounding rule and names
        a fabricated function (e.g. processAndStoreImage that doesn't exist anywhere
        in the codebase). Fabricated names would otherwise be handed to the Fix
        Generation Agent, which would target a non-existent symbol.
        """
        ungrounded: list[str] = []

        if result.affected_function:
            if not await self._symbol_exists_in_repo(result.affected_function):
                ungrounded.append(result.affected_function)
                logger.warning(
                    "DiagnosisAgent: affected_function '%s' not found in %s/%s — "
                    "treating as ungrounded and escalating",
                    result.affected_function, self._owner, self._repo,
                )
                result.affected_function = None
                result.affected_file = None

        if result.additional_fix_function:
            if not await self._symbol_exists_in_repo(result.additional_fix_function):
                ungrounded.append(result.additional_fix_function)
                logger.warning(
                    "DiagnosisAgent: additional_fix_function '%s' not found in %s/%s",
                    result.additional_fix_function, self._owner, self._repo,
                )
                result.additional_fix_function = None
                result.additional_fix_file = None

        # Verify file paths independently — the LLM may hallucinate a plausible-looking
        # file path even when the function name passes the symbol search.
        for attr in ("affected_file", "additional_fix_file"):
            path = getattr(result, attr)
            if path and not await self._file_exists_in_repo(path):
                logger.warning(
                    "DiagnosisAgent: %s '%s' not found in %s/%s — nulling",
                    attr, path, self._owner, self._repo,
                )
                ungrounded.append(path)
                setattr(result, attr, None)
                fn_attr = "affected_function" if attr == "affected_file" else "additional_fix_function"
                setattr(result, fn_attr, None)

        # Verify blast_radius entries — each has a "file" key that may be hallucinated.
        if result.blast_radius:
            verified = []
            dropped = []
            for entry in result.blast_radius:
                path = entry.get("file", "")
                if not path or await self._file_exists_in_repo(path):
                    verified.append(entry)
                else:
                    dropped.append(path)
                    logger.warning(
                        "DiagnosisAgent: blast_radius file '%s' not found in %s/%s — removing entry",
                        path, self._owner, self._repo,
                    )
            result.blast_radius = verified
            if dropped:
                ungrounded.extend(dropped)

        if ungrounded:
            note = (
                f"GROUNDING FAILURE: function name(s) {ungrounded} not found in "
                f"{self._owner}/{self._repo} — diagnosis reasoning may be sound but the "
                f"code target was not verified. Escalating for human review."
            )
            result.evidence = [*result.evidence, note]
            result.confidence = min(result.confidence, 0.65)
            result.escalate = result.confidence < CONFIDENCE_THRESHOLD

        # ----- Prose scan -----------------------------------------------
        # Structured fields can be nulled but the same fabricated names often
        # leak into root_cause / fix_approach / additional_fix prose, where
        # downstream agents still read them. Verify any function-shaped tokens
        # there too.
        prose = " ".join(filter(None, [
            result.root_cause,
            result.fix_approach,
            result.additional_fix,
        ]))
        candidates = _extract_prose_symbols(prose)

        # Skip names already resolved above (verified-good or already-flagged-bad).
        already_seen: set[str] = set(ungrounded)
        for n in (result.affected_function, result.additional_fix_function):
            if n:
                already_seen.add(n)
        to_check = [n for n in candidates if n not in already_seen][:_MAX_PROSE_CANDIDATES]

        if to_check:
            existence = await asyncio.gather(
                *(self._symbol_exists_in_repo(n) for n in to_check),
                return_exceptions=False,
            )
            prose_unverified = [n for n, exists in zip(to_check, existence) if not exists]
            if prose_unverified:
                logger.warning(
                    "DiagnosisAgent: prose names unverified symbol(s) %s in %s/%s",
                    prose_unverified, self._owner, self._repo,
                )
                result.evidence = [
                    *result.evidence,
                    (
                        f"PROSE GROUNDING WARNING: symbol(s) {prose_unverified} cited in "
                        f"reasoning do not exist in {self._owner}/{self._repo}. The diagnosis "
                        f"narrative may be hallucinated even where structured fields look clean."
                    ),
                ]
                # Tighter cap than structured (0.65) because hallucinating function
                # names mid-reasoning means the explanation itself is suspect, not
                # just the target slot.
                result.confidence = min(result.confidence, 0.55)
                result.escalate = result.confidence < CONFIDENCE_THRESHOLD

        # ----- Blast radius coverage check ------------------------------
        # Empty blast_radius is honest when the function is a top-level
        # handler / route. But for a typical helper, missing callers means
        # the model didn't actually do the search step — surface as an
        # evidence note so reviewers know the constraint set is incomplete.
        # Don't cap confidence: this is an observability nudge, not a
        # correctness gate.
        if (
            result.affected_function
            and not result.blast_radius
            and not _looks_like_entry_point(result.affected_function, result.evidence)
        ):
            result.evidence = [
                *result.evidence,
                (
                    f"BLAST RADIUS WARNING: no callers reported for "
                    f"'{result.affected_function}'. Either it's an entry point "
                    f"(route/handler/cron — note that in evidence) or the "
                    f"diagnosis skipped the caller search."
                ),
            ]

        return result

    async def diagnose(self, incident: IncidentState, prior_context: str | None = None) -> DiagnosisResult:
        """Run diagnosis on a triaged incident. Returns a DiagnosisResult."""
        event = incident.error_event
        log_group = event.metadata.get("log_group", "")
        pattern = event.metadata.get("pattern", event.error_type or event.title)

        log_group_warning = ""
        if not log_group:
            logger.warning("DiagnosisAgent: log_group missing from incident metadata — log-based steps will produce no results")
            log_group_warning = (
                "\nWARNING: log_group is not set for this incident. Steps 1–3 (log tools) will "
                "return no data. Skip them and proceed directly to steps 4–6 (knowledge base + codebase search). "
                "Set reproduction_confirmed=false and cap confidence at 0.75.\n"
            )

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
{log_group_warning}{prior_section}
STEPS — call tools in this exact order. Do not skip steps 1–3 unless log_group is missing.
Complete each step before moving to the next.

1. get_error_samples — see the actual error messages
   log_group="{log_group}", pattern="{pattern}", minutes=120
   → Extract: exact error text, function names in stack trace, any file paths or line numbers.
     These become your search terms for step 5.

2. check_still_occurring — confirm if error is ongoing
   log_group="{log_group}", pattern="{pattern}"

3. get_occurrence_timeline — understand the trend
   log_group="{log_group}", pattern="{pattern}", hours=24

4. search_similar_incidents — check knowledge base
   symptoms = domain-specific keywords from the error: operation names, error codes, component names.
   Do NOT use generic words like "error", "null", "undefined" — they match everything.
   Good: "E11000 duplicate insertMany parallel workers"
   Bad:  "TypeError undefined cannot read property"

5. search_codebase — find candidate file paths
   Use specific terms from the stack trace (function names, file paths) found in step 1,
   NOT just the raw error type. If the stack trace shows `insertMany appmasterreferrals`,
   query that. If it shows `classifyFields`, query that function name.

6. get_file_contents — fetch the FULL source of the file(s) identified in step 5.
   RAG returns 400-char fragments — you MUST read the full file to understand the code.
   Do not stop at one file. If that file spawns workers, calls helpers, or delegates to
   other modules that are part of the failure path, read those too.

   At each file, ask:
     - What data enters this function and where does it come from?
     - What assumptions does this code make that the error shows are violated?
     - Does this function call something else that is part of the failure path?
     - Are there naming clues (function names, variable names, comments) that reveal intent?
     - For parallel/concurrent code: can two execution paths touch the same data simultaneously?
   Keep reading until you can explain the failure completely from first principles.

   If a file is truncated, search for the specific function name via search_codebase.

7. verify_symbol_in_repo — MANDATORY CODE-GROUNDING STEP (do not skip).
   For EVERY function name you intend to put in affected_function or additional_fix_function,
   call verify_symbol_in_repo with that exact name. This is not optional, even if the name
   "obviously" should exist or "matches the naming convention".

   Two valid outcomes:
     a) FOUND  → the symbol is real; you may use it as affected_function and the file path
                 reported by the tool as affected_file. Prefer that path over any guess.
     b) NOT_FOUND → the symbol does not exist on the default branch. You MUST:
                 - set affected_function (or additional_fix_function) to null,
                 - cap confidence at 0.65,
                 - in `evidence`, list candidate file paths you read (from steps 5–6) that
                   most likely contain the real producer, plus the search terms a human
                   should grep for (e.g. distinctive S3 key prefixes, MIME-type checks,
                   library symbols from the stack trace),
                 - in `fix_approach`, describe WHAT must change conceptually, not WHERE.
   Naming a symbol you have not verified is a hallucination. Do not do it.

   This applies to PROSE FIELDS too — root_cause, fix_approach, additional_fix.
   Do not name a function in those narrative fields unless verify_symbol_in_repo
   returned FOUND for it. The runtime re-checks every camelCase function name in
   the prose; unverified names cap confidence further and escalate. If you don't
   know the real symbol, describe the failing operation in plain English (e.g.
   "the Vision API caller", "the function that uploads the derived image to S3")
   and list candidate file paths in `evidence`.

8. Answer with a JSON diagnosis.

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

STEP 3 — write root_cause and fix_approach based solely on what you read in the code.
  root_cause MUST name the DIRECT PRODUCER function and which return path omits the field.
  fix_approach MUST fix the upstream source — do NOT suggest null guards, optional chaining,
  or try/catch at the crash site. Those hide the problem instead of fixing it.

- affected_function and affected_file identify the PRIMARY root cause location (upstream, not crash site).
- Both must be GROUNDED via verify_symbol_in_repo (step 7). If verification returned NOT_FOUND
  for the function, set both affected_function AND affected_file to null rather than guessing.
- If TWO changes are needed, put the upstream fix in affected_function/affected_file and
  describe the secondary fix in additional_fix. additional_fix_function must also be grounded.

PRE-FIX REASONING — populate `blast_radius` and `contract_change`:

After identifying affected_function, search the repo for callers of that
function (use search_codebase with the function name). For each distinct
caller you find, add an entry to `blast_radius`:

  - `file`: path of the caller (must be a real file you observed)
  - `function`: the calling function or "(top-level)" for module-scope calls
  - `snippet`: ≤200-char excerpt showing how the caller uses the function

Aim for 3–8 entries. If the function has no callers (it's a top-level
handler / route / cron entry point), return an empty list and explain in
evidence ("affected_function is a route handler — no callers"). Empty
list is honest; fabricating callers is not.

`contract_change` describes whether your proposed fix changes the
function's external behaviour:
  - "none": signature, return type, and side effects are unchanged
  - "signature": parameter list / types change
  - "return_type": return shape changes
  - "side_effect": new I/O, new exceptions thrown, new mutations, etc.
If non-"none", populate `contract_change_detail` with one short
sentence describing what changes. The fix agent will surface this
loudly so every caller is updated.

Answer with ONLY a valid JSON object:
{{
  "root_cause": "precise description of WHY the error occurs — name the upstream cause",
  "confidence": 0.82,
  "evidence": [
    "specific fact from logs or code that supports root cause",
    "another concrete observation"
  ],
  "fix_approach": "what must change at the upstream source",
  "affected_function": "primaryFunctionToFix or null",
  "affected_file": "path/to/primary/file.js or null",
  "additional_fix": "optional: describe any secondary change in a different function/file, or null",
  "additional_fix_function": "secondaryFunctionName or null",
  "additional_fix_file": "path/to/secondary/file.js or null",
  "reproduction_confirmed": true,
  "blast_radius": [
    {{"file": "path/to/caller.js", "function": "callerFunction", "snippet": "const x = primaryFunctionToFix(...)"}}
  ],
  "contract_change": "none",
  "contract_change_detail": null
}}

Confidence guide:
  0.90+ → near certain, clear evidence in code + logs, AND affected_function verified FOUND
  0.70-0.90 → probable, strong log evidence but limited code visibility
  0.50-0.70 → possible, pattern matches but incomplete evidence
  <0.50 → uncertain, escalate to human
  If log_group was missing and steps 1–3 returned no data, cap confidence at 0.75.
  If any named function returned NOT_FOUND in step 7, cap confidence at 0.65 and set the
  corresponding *_function/*_file fields to null."""

        result = await self.run(prompt)
        parsed = _parse_diagnosis_result(result.answer)
        if parsed.root_cause == _PARSE_FAILURE_ROOT_CAUSE:
            # One retry with an explicit reminder. Most parse failures are
            # one-shot drift (extra prose, dropped closing fence on a
            # truncated reply); a stricter prompt usually clears it.
            retry_prompt = (
                prompt
                + "\n\nIMPORTANT: Your previous response could not be parsed."
                  " Return ONLY a single valid JSON object matching the schema above."
                  " No markdown fences, no commentary, no trailing text."
            )
            logger.info("DiagnosisAgent retrying after parse failure")
            result = await self.run(retry_prompt)
            parsed = _parse_diagnosis_result(result.answer)
        return await self._enforce_grounding(parsed)
