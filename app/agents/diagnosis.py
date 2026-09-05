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
  7. verify_symbol_in_repo  → confirm any named function actually exists
  8. submit_diagnosis       → the ONLY way to finalize. Every grounding-relevant
                              field (affected_file/root_cause_snippet pairing,
                              every additional_fix_targets/blast_radius entry,
                              any file named in prose) is checked inline before
                              acceptance — a failed check rejects the tool call
                              with a specific reason and the model retries in the
                              same conversation, rather than the old design (a
                              free-text JSON answer parsed and grounded only
                              after the fact — see git history for that version).

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
from app.services.ipi_guard import scan_for_injection, wrap_untrusted
from app.services.llm import LLMService
from app.services.rag import RAGService
from app.services.repo import LocalRepoService

logger = logging.getLogger(__name__)

try:
    from app.services.code_graph.graph import CodeGraph
    _code_graph = CodeGraph.load_from_store()
except Exception as _cg_err:
    from app.services.code_graph.graph import CodeGraph
    _code_graph = CodeGraph()
    logger.warning("[DiagnosisAgent] Call graph unavailable: %s", _cg_err)

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


_PASCAL_CASE_RE = re.compile(r"\b[A-Z][A-Za-z0-9]*\b")

# Matches a plausible repo-relative source file mention in prose, e.g.
# "brandAInterviewUsers.js" or "routes/api/foo.js" — used to catch
# additional_fix prose asserting a specific file is still vulnerable with no
# grounded additional_fix_file/_snippet behind the claim (see _enforce_grounding).
_FILE_MENTION_RE = re.compile(r"\b[\w./-]+\.(?:js|ts|jsx|tsx|py)\b")


def _snippet_skeleton(text: str) -> str:
    """Normalize a code snippet for fuzzy containment checks.

    Strips PascalCase identifiers (Mongoose model names, class names -- the one
    thing legitimately different between brand-specific sibling files copy-pasting
    the same handler) and collapses whitespace. Two snippets that are the same
    handler with only the model name swapped normalize to the same skeleton;
    snippets with a genuinely different route, method, or structure don't.
    """
    return re.sub(r"\s+", " ", _PASCAL_CASE_RE.sub("", text or "")).strip()


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


# Matches a full V8 stack-frame line in one shot, rather than finding the path
# and then regex-searching backward over an arbitrary-width window for a
# function name -- an earlier version of this did exactly that with a fixed
# 40-char lookback, which was fragile: whether "at fnName (" fell inside or
# outside that window depended on how much text (e.g. a long function name,
# or "async ") sat between "at" and the path, giving inconsistent results for
# semantically identical frames. One regex covers every real shape seen in
# this app's traces:
#   "at fnName (/app/path.js:L:C)"        -> function = fnName
#   "at async fnName (/app/path.js:L:C)"  -> function = fnName
#   "at async /app/path.js:L:C"           -> function = None (no parens at all)
#   "/app/path.js:L"  (bare throw-site line, no "at" prefix)  -> function = None
# Anchored on the literal "/app/" prefix -- that's what lets us tell "this is
# the container's own code" apart from "this happens to look like a path in
# some unrelated log text".
_STACK_FRAME_RE = re.compile(
    r"(?:at\s+(?:async\s+)?(?:([A-Za-z_$][\w$.]*)\s*\(\s*)?)?"
    r"/app/([\w./\-]+\.(?:js|mjs|cjs|ts|tsx))\b"
)


def extract_stack_trace_paths(text: str) -> list[dict]:
    """Deterministically pull this app's own file paths out of raw error text.

    Real motivation: 3 of 4 real incidents checked this session already had
    the exact file (and often the function) sitting verbatim in the stack
    trace -- e.g. "at checkPrankForInterview (/app/constants/prankCheckerMain.js:296:44)".
    Calling search_codebase (an embedding search with a similarity threshold)
    to "find" a file that's already spelled out in the input is a wasted
    ReAct-loop turn at best, and a miss at worst if the vector index is stale
    or the match scores just under min_score. This finds those cases directly,
    with zero external dependency (no RAG, no embeddings) and no similarity
    threshold to miss.

    Filters out /app/node_modules/** -- those paths match the same "/app/"
    prefix (the container installs dependencies under /app too) but are
    vendored library code, not this app's own code, and diagnosing a bug
    "in" a dependency's internals is never the right answer here.

    Order is preserved (first occurrence = closest to the actual throw site
    in a typical V8 trace dump) and paths are deduped, keeping the first
    associated function name seen for each.

    Returns: [{"file": "routes/api/image.js", "function": "someFn" | None}, ...]
    Empty list is the normal, expected result for traceless input (e.g. a
    bare DeprecationWarning) -- callers should fall back to search_codebase/
    grep_codebase in that case, same as today.
    """
    if not text:
        return []
    seen: dict[str, str | None] = {}
    for m in _STACK_FRAME_RE.finditer(text):
        fn, path = m.group(1), m.group(2)
        if "node_modules/" in path:
            continue
        # Same file can appear twice in one V8 dump -- a bare throw-site line
        # ("/app/foo.js:296") with no function name, then again in the "at
        # fnName (/app/foo.js:296:44)" stack frame. Keep the first occurrence
        # for ordering, but don't let a function-name-less first sighting
        # permanently blank out a real name a later occurrence provides.
        if path not in seen or (seen[path] is None and fn is not None):
            seen[path] = fn
    return [{"file": path, "function": fn} for path, fn in seen.items()]


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
    root_cause_snippet: str | None = None      # verbatim excerpt of the CURRENT code in
    # affected_file that actually shows the claimed bug -- the PRIMARY-target counterpart
    # to additional_fix_snippet below. Real production bug, the worst fabrication found
    # this session: a diagnosis named affected_file="models/MasterBrandA.js" and quoted
    # a root_cause code snippet ("errors: { flagged: {...}, contentFlags: {...} }") that
    # exists NOWHERE in the real repo -- not even in a different file. The real bug was a
    # single, unrelated file (a log model with its own genuinely-real `errors` field) that
    # was never found at all. affected_file's existence-only check passed trivially (the
    # named file is real, just doesn't contain what root_cause claims), and unlike
    # additional_fix_file, affected_file had no snippet-grounding check whatsoever --
    # backwards, since affected_file is the PRIMARY field that actually gets edited by
    # FixGenerationAgent, making it the single most consequential field to leave
    # ungrounded. See _enforce_grounding.
    additional_fix: str | None = None          # secondary change description
    additional_fix_function: str | None = None # secondary function name
    additional_fix_file: str | None = None     # secondary file path
    additional_fix_snippet: str | None = None  # excerpt of the vulnerable code CURRENTLY
    # in additional_fix_file, so _enforce_grounding can verify it's still actually there
    # (see _snippet_is_grounded) instead of just checking the file exists. Without this,
    # a claim like "still broken in brandAInterviewUsers.js" sails through ungrounded
    # even when that file was fixed by an earlier, separate incident — the exact failure
    # mode blast_radius entries were already protected against, replayed through this
    # sibling field instead.
    additional_fix_targets: list[dict] = field(default_factory=list)
    # ^ each entry: {"file": str, "function": str|None, "snippet": str|None}. The
    # multi-file counterpart to additional_fix_file/_function/_snippet above. Real
    # production bug: a diagnosis correctly identified 3 sibling files needing the
    # identical per-brand-duplication fix in its PROSE, but additional_fix_file can
    # only ever carry ONE — FixGenerationAgent structurally never had a path to attempt
    # more than one secondary fix, even when the diagnosis got every file right.
    # additional_fix_file/_function/_snippet remain for the single-file case (e.g.
    # "this other top-level entry point also needs the fix"); use this list instead
    # whenever more than one sibling file is genuinely affected. Grounded the same way
    # as blast_radius entries — see _enforce_grounding.
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
    grounding_rejections: int = 0
    # ^ how many submit_diagnosis attempts were rejected before this one succeeded
    # (or, on the fail-closed fallback, before the agent gave up entirely). Set in
    # diagnose() from DiagnosisAgent._rejection_count -- see measure_diagnosis_grounding.py.


def _parse_file_entries(raw: object) -> list[dict]:
    """Normalise a {file, function, snippet} entry list from parsed JSON.

    Shared by blast_radius and additional_fix_targets — both use the identical shape.
    Drops entries missing a usable file path rather than raising.
    """
    entries: list[dict] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict) and entry.get("file"):
                entries.append({
                    "file": str(entry["file"]),
                    "function": str(entry.get("function", "") or ""),
                    "snippet": str(entry.get("snippet", "") or "")[:400],
                })
    return entries


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
        local_repo: LocalRepoService | None = None,
    ) -> None:
        super().__init__(llm=LLMService())   # Sonnet — default model
        self._aws = aws or AWSService()
        self._rag = rag
        self._github = github or GitHubService()
        _owner, _repo = settings.fix_target_repo.split("/", 1)
        self._owner = _owner
        self._repo = _repo
        # Overridable so replay/eval tooling (scripts/eval_diagnosis_regression.py)
        # can pin diagnosis to an isolated historical worktree instead of the
        # live shared clone's current HEAD — see LocalRepoService(pinned_sha=...).
        self._local_repo = local_repo or LocalRepoService(self._owner, self._repo)
        self._register_tools()
        # A real production diagnosis once answered in a single LLM call
        # with zero tool calls, fabricating file/schema content it never
        # looked at (_enforce_grounding() catches some shapes of this
        # after the fact, but can't catch fabricated content when no
        # symbol/file pairing was even claimed). Require at least one real
        # tool call — evidence of *some* kind — before an answer is
        # accepted at all.
        self._min_tool_calls_before_answer = 1
        # The count floor above isn't enough on its own. Real production bug:
        # a diagnosis called get_error_samples and check_still_occurring (both
        # log-checking tools) and both returned no data, satisfying "at least
        # 1 real tool call" -- then answered with a fabricated affected_file
        # and root_cause_snippet, having never once called a tool that reads
        # actual code. With no real code investigated, it filled the gap by
        # inventing a plausible-sounding root cause (in that incident,
        # reaching for AGENTS.md's per-brand MODEL_MAP pattern, which had
        # nothing to do with the actual bug). Require at least one call to an
        # actual code-reading tool before an answer is accepted -- log/timeline
        # tools establish whether the error is occurring, they can't establish
        # WHY.
        self._required_tool_names_before_answer = {
            "get_file_contents", "search_codebase", "grep_codebase",
        }
        # Real production bug found via a SWE-bench eval: a diagnosis correctly
        # identified the actual right file, wrote a detailed, confident
        # write-up ending in "Status: Diagnosis accepted" -- then never once
        # called submit_diagnosis. The three-tool set above was satisfied (it
        # read real code), so the loop happily accepted the free-text answer;
        # diagnose() then saw self._diagnosis_submitted is still None and
        # discarded a correct diagnosis as confidence=0.0/escalate=True.
        self._must_call_before_answer = "submit_diagnosis"  # for the rejection message
        # A second, deeper layer of the same bug: gating on "was submit_diagnosis
        # ever called" is satisfied by a REJECTED call too. Check success, not
        # attempt -- self._diagnosis_submitted is only ever set by
        # _submit_diagnosis once a submission actually passes grounding. See
        # _must_call_check's docstring in BaseAgent.__init__.
        self._must_call_check = lambda: self._diagnosis_submitted is not None
        # Set by the submit_diagnosis tool handler once a submission passes every
        # grounding check. diagnose() reads this after self.run() returns instead
        # of parsing the free-text Answer as JSON -- grounding now gates finalizing
        # the diagnosis, not something that happens to the answer after the fact.
        # None means the model never got a submission through (see diagnose()'s
        # fail-closed fallback). Reset at the top of every diagnose() call.
        self._diagnosis_submitted: DiagnosisResult | None = None
        # Counts rejected submit_diagnosis attempts within one diagnose() call --
        # 0 means the first submission was already grounded. Feeds
        # DiagnosisResult.grounding_rejections -> incident.diagnosis_grounding_rejections
        # -> scripts/measure_diagnosis_grounding.py's rejection-rate metric. Reset at
        # the top of every diagnose() call, same as _diagnosis_submitted.
        self._rejection_count = 0

    def _register_tools(self) -> None:
        aws = self._aws
        rag = self._rag
        github = self._github
        owner = self._owner
        repo = self._repo
        # The target app's logs live in a different AWS region than agent-platform's
        # own infra (us-east-2 vs us-east-1) -- get_error_logs() already threads
        # this through everywhere else, but search_log_events() (used by all three
        # log tools below) never had a region param at all, so every call here
        # silently queried the wrong region and failed. See search_log_events's
        # docstring for the full story.
        log_region = settings.ecs_log_groups_region or None

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
                    region=log_region,
                )
                if not events:
                    return f"No '{pattern}' events in the last {minutes} min."
                lines = [f"[{e['timestamp']}] stream={e['stream'].rsplit('/', 1)[-1][:8]}... {e['message'][:200]}"
                         for e in events]
                raw = f"{len(events)} sample(s):\n" + "\n".join(lines)
                scan_for_injection(raw, source="cloudwatch-logs")
                return wrap_untrusted(raw, source="cloudwatch-logs")
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
                    region=log_region,
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
                    region=log_region,
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
                chunks = await rag.hybrid_search(query, n_results=4, min_score=0.45)
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
            raw = "\n\n".join(parts)
            scan_for_injection(raw, source="rag-codebase-search")
            return wrap_untrusted(raw, source="rag-codebase-search")

        async def _get_file_contents(file_path: str) -> str:
            """Fetch the full source of a file from the target repo."""
            try:
                content, _ = await github.get_file_contents(owner, repo, file_path.lstrip("/"))
                if len(content) > 12000:
                    content = (
                        content[:12000]
                        + f"\n\n[TRUNCATED — file is {len(content)} chars, only first 12000 shown. "
                        f"If the function you need is not visible, call get_file_contents again "
                        f"with a more specific path or search for the function name via search_codebase.]"
                    )
                scan_for_injection(content, source=f"github-file:{file_path}")
                return wrap_untrusted(content, source=f"github-file:{file_path}")
            except Exception as exc:
                return f"Could not fetch {file_path}: {exc}"

        async def _grep_codebase(pattern: str, file_glob: str = "*.js") -> str:
            """Exact-string search across every file in the local repo clone.

            Returns each matching line with its file path and line number.
            Use this when you need to find WHERE a specific function or string is
            called/defined — e.g. 'mongoose.connect' or 'require(\"mongoose\")'.
            Faster and more precise than search_codebase for exact patterns.
            Input: {pattern: string, file_glob: string (default '**/*.js')}
            """
            local_repo = self._local_repo
            if not local_repo.ready:
                return "Local repo not available — use search_codebase instead."
            import fnmatch
            matches: list[str] = []
            try:
                for rel_path in sorted(local_repo.list_files()):
                    if not fnmatch.fnmatch(rel_path, file_glob):
                        continue
                    try:
                        content = local_repo.read_file(rel_path)
                    except Exception:
                        continue
                    for i, line in enumerate(content.splitlines(), 1):
                        if pattern in line:
                            matches.append(f"{rel_path}:{i}: {line.strip()[:120]}")
                            if len(matches) >= 50:
                                break
                    if len(matches) >= 50:
                        break
            except Exception as exc:
                return f"grep_codebase error: {exc}"
            if not matches:
                return f"No matches for '{pattern}' in {file_glob}."
            return f"{len(matches)} match(es) for '{pattern}':\n" + "\n".join(matches)

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
            "grep_codebase",
            _grep_codebase,
            (
                "Exact-string search across every file in the local repo clone. "
                "Returns file paths and line numbers for every match. "
                "Use this instead of search_codebase when you need to find WHERE a specific "
                "string appears — e.g. 'mongoose.connect', 'require(\"mongoose\")', a function "
                "call, or an import. search_codebase is semantic/fuzzy; grep_codebase is exact. "
                "Input: {pattern: string, file_glob: string (default '*.js', matches all .js files at any depth)}"
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

        async def _find_callers(function_name: str) -> str:
            """Look up every caller of a function in the call graph index."""
            callers = _code_graph.find_callers(function_name)
            if not callers:
                return (
                    f"No callers found for '{function_name}' in the call graph index. "
                    f"Either it is an entry point (route/handler/cron) or the index has not been built. "
                    f"Fall back to search_codebase to find callers manually."
                )
            lines = [f"Callers of `{function_name}` ({len(callers)} found):"]
            for c in callers[:15]:
                lines.append(f"  {c.file_path} → {c.function_name} (line {c.line})")
            if len(callers) > 15:
                lines.append(f"  ... and {len(callers) - 15} more")
            return "\n".join(lines)

        self.register_tool(
            "find_callers",
            _find_callers,
            (
                "Query the call graph index to find every function that calls the target function. "
                "Returns file paths, caller function names, and line numbers. "
                "Use this for blast radius analysis BEFORE deciding on a fix — it gives the complete "
                "picture in one call, unlike search_codebase which only returns partial results. "
                "If no results, fall back to search_codebase. "
                "Input: {function_name: string (e.g. 'classifyFields')}"
            ),
        )

        async def _submit_diagnosis(**kwargs) -> str:
            problems = await self._validate_diagnosis_submission(kwargs)
            if problems:
                self._rejection_count += 1
                logger.info(
                    "DiagnosisAgent: submit_diagnosis rejected (attempt %d): %s",
                    self._rejection_count, "; ".join(problems),
                )
                return (
                    "REJECTED — fix the following and call submit_diagnosis again:\n"
                    + "\n".join(f"- {p}" for p in problems)
                )
            confidence = float(kwargs.get("confidence", 0.5))
            contract_change = str(kwargs.get("contract_change", "none") or "none").lower()
            if contract_change not in ("none", "signature", "return_type", "side_effect"):
                contract_change = "none"
            self._diagnosis_submitted = DiagnosisResult(
                root_cause=kwargs.get("root_cause", "Unknown"),
                confidence=confidence,
                evidence=list(kwargs.get("evidence") or []),
                fix_approach=kwargs.get("fix_approach", ""),
                affected_function=kwargs.get("affected_function"),
                affected_file=kwargs.get("affected_file"),
                root_cause_snippet=kwargs.get("root_cause_snippet"),
                additional_fix=kwargs.get("additional_fix"),
                additional_fix_function=kwargs.get("additional_fix_function"),
                additional_fix_file=kwargs.get("additional_fix_file"),
                additional_fix_snippet=kwargs.get("additional_fix_snippet"),
                additional_fix_targets=_parse_file_entries(kwargs.get("additional_fix_targets")),
                reproduction_confirmed=bool(kwargs.get("reproduction_confirmed", False)),
                escalate=confidence < CONFIDENCE_THRESHOLD,
                blast_radius=_parse_file_entries(kwargs.get("blast_radius")),
                contract_change=contract_change,
                contract_change_detail=kwargs.get("contract_change_detail"),
                raw_llm=json.dumps(kwargs, default=str),
                grounding_rejections=self._rejection_count,
            )
            return "Diagnosis accepted. Write a brief final Answer to finish (e.g. \"Answer: Diagnosis submitted.\")."

        self.register_tool(
            "submit_diagnosis",
            _submit_diagnosis,
            (
                "Finalize your diagnosis. Every grounding-relevant field is checked before this "
                "is accepted — root_cause_snippet must be verbatim from affected_file, every "
                "additional_fix_targets entry needs its own real snippet, and any file named in "
                "root_cause/additional_fix text must have a matching structured entry. A "
                "rejection tells you exactly what's wrong — re-verify with your other tools and "
                "call this again. This is the ONLY way to finalize; do not write the diagnosis "
                "as JSON in your Answer. "
                "Input: {root_cause: string, confidence: float, evidence: [string], "
                "fix_approach: string, affected_function: string|null, affected_file: string|null, "
                "root_cause_snippet: string|null, additional_fix: string|null, "
                "additional_fix_function: string|null, additional_fix_file: string|null, "
                "additional_fix_snippet: string|null, "
                "additional_fix_targets: [{file, function, snippet}], "
                "reproduction_confirmed: bool, blast_radius: [{file, function, snippet}], "
                "contract_change: 'none'|'signature'|'return_type'|'side_effect', "
                "contract_change_detail: string|null}"
            ),
        )

    async def _ensure_local_repo(self) -> bool:
        """Ensure the local clone is fresh. Returns True on success, False on failure."""
        try:
            await self._local_repo.ensure_fresh()
            logger.info(
                "DiagnosisAgent: local repo ready for %s/%s (%d files)",
                self._owner, self._repo, len(self._local_repo.list_files()),
            )
            return True
        except Exception as exc:
            logger.warning(
                "DiagnosisAgent: could not prepare local repo for %s/%s — grounding checks will be skipped: %s",
                self._owner, self._repo, exc,
            )
            return False

    async def _file_exists_in_repo(self, path: str) -> bool:
        """Check if a file exists in the local clone. Fails open if clone unavailable."""
        p = (path or "").strip().lstrip("/")
        if not p:
            return False
        if not self._local_repo.ready:
            return True  # can't verify — assume it exists
        return self._local_repo.file_exists(p)

    async def _snippet_is_grounded(self, path: str, snippet: str) -> bool:
        """Check whether a blast_radius snippet's actual code shape appears in `path`.

        _file_exists_in_repo only confirms the FILE is real -- it says nothing about
        whether the snippet is. Confirmed in production: a diagnosis correctly read
        brandCInterviewUsers.js and found its real, still-vulnerable handler, then listed
        3 sibling files (brandAInterviewUsers.js, brandBInterviewUsers.js,
        brandDInterviewUsers.js) as having "the identical missing
        guard" with detailed, plausible-looking snippets -- one per file, each just
        the brandCInterviewUsers.js snippet with the Mongoose model name swapped. All 3
        files are real and all 3 snippets passed the file-existence check. All 3
        were also completely fabricated: those files were fixed via earlier, separate
        incidents and no longer contain anything resembling that code -- different
        route path, different query method, different structure entirely. The model
        pattern-completed a plausible sibling from the one file it actually read,
        rather than calling read_file on the other three, and nothing caught it.

        Not a verbatim match (that would reject legitimate paraphrasing / minor
        formatting differences) -- uses _snippet_skeleton to tolerate exactly the one
        thing that legitimately differs between real sibling files (the model name)
        while still rejecting a snippet whose route, method, or structure isn't
        actually present anywhere in the file.
        """
        if not self._local_repo.ready:
            return True  # can't verify — fail open, same policy as _file_exists_in_repo
        try:
            content = self._local_repo.read_file(path)
        except Exception:
            return True  # read failed for a reason unrelated to the snippet — don't punish it
        skeleton = _snippet_skeleton(snippet)
        if len(skeleton) < 20:
            return True  # too short to meaningfully verify — avoid false positives
        return skeleton in _snippet_skeleton(content)

    async def _symbol_exists_in_repo(self, symbol: str) -> bool:
        """Check whether `symbol` appears in the target repo.

        Authoritative grounding check: the LLM may invent function names that look
        plausible but don't exist. We re-verify post-parse so a fabricated name can't
        leak into the Fix Generation Agent.

        Live diagnosis checks GitHub Code Search against the default branch (fast,
        no local-clone dependency). When self._local_repo is pinned to a historical
        SHA instead — replay/eval tooling, see scripts/eval_diagnosis_regression.py —
        GitHub Code Search is the wrong check: it only ever searches the current
        default branch, which by definition doesn't match a historical commit
        (found live: every case in the regression-eval replay degraded to escalate
        because a symbol that legitimately existed at the pre-fix SHA no longer
        matched current main). Grep the pinned worktree directly in that case.
        """
        name = (symbol or "").strip()
        if not name:
            return False

        if self._local_repo.pinned and self._local_repo.ready:
            needles = (f"{name}(", f'"{name}"')
            for rel_path in self._local_repo.list_files():
                try:
                    content = self._local_repo.read_file(rel_path)
                except Exception:
                    continue
                if any(n in content for n in needles):
                    return True
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

    async def _validate_diagnosis_submission(self, data: dict) -> list[str]:
        """Validate one submit_diagnosis attempt. Returns a list of problems —
        empty means the submission is grounded and can be accepted.

        This is what used to be _enforce_grounding's structural checks
        (function/file existence, file<->function pairing, verbatim snippet
        matching on the primary claim, every blast_radius/additional_fix_targets
        entry, additional_fix_file/_snippet) run BEFORE acceptance instead of
        after — a failure here becomes a same-turn tool rejection the model
        sees and can act on, not a silent null-and-cap the model never sees.
        Same underlying helpers as before (_symbol_exists_in_repo,
        _file_exists_in_repo, _snippet_is_grounded) — only the timing and the
        outcome on failure changed.
        """
        problems: list[str] = []
        # Note: no blanket "not self._local_repo.ready" bypass here — function-name
        # existence (_symbol_exists_in_repo) uses GitHub Code Search, not the local
        # clone, so it must still run even when the clone isn't ready. Each
        # file/snippet-based check below fails open internally via its own helper
        # (_file_exists_in_repo / _snippet_is_grounded) instead.
        affected_file = data.get("affected_file")
        affected_function = data.get("affected_function")
        root_cause_snippet = data.get("root_cause_snippet")

        for fn_attr in ("affected_function", "additional_fix_function"):
            fn = data.get(fn_attr)
            if fn and not await self._symbol_exists_in_repo(fn):
                problems.append(
                    f"{fn_attr} '{fn}' not found in {self._owner}/{self._repo}. "
                    f"Call verify_symbol_in_repo to confirm, or set {fn_attr} to null."
                )

        file_ok = True
        if affected_file and not await self._file_exists_in_repo(affected_file):
            file_ok = False
            problems.append(
                f"affected_file '{affected_file}' does not exist in {self._owner}/{self._repo}. "
                f"Re-check the path via search_codebase or grep_codebase."
            )
        elif affected_file and affected_function:
            try:
                content = self._local_repo.read_file(affected_file) if self._local_repo.ready else None
            except Exception:
                content = None
            if content is not None and affected_function not in content:
                file_ok = False
                problems.append(
                    f"'{affected_function}' does not appear inside '{affected_file}' — both exist "
                    f"in the repo independently, but not together. Re-read the file to confirm "
                    f"the function actually lives there, or correct the pairing."
                )

        # Snippet-mandatory checks below are explicitly gated on self._local_repo.ready
        # (not just relying on _snippet_is_grounded's own internal fail-open) because a
        # MISSING snippet needs the same "can't verify, don't punish" treatment as a
        # present-but-unverifiable one -- _snippet_is_grounded only fails open once
        # called, but `bool(snippet) and ...` short-circuits before ever calling it.
        if affected_file and file_ok and self._local_repo.ready:
            if not root_cause_snippet or not await self._snippet_is_grounded(affected_file, root_cause_snippet):
                problems.append(
                    f"root_cause_snippet is missing or doesn't match {affected_file}'s actual "
                    f"current content. Call get_file_contents on {affected_file} and copy a real "
                    f"excerpt showing the claimed bug."
                )

        additional_fix_file = data.get("additional_fix_file")
        additional_fix_snippet = data.get("additional_fix_snippet")
        if additional_fix_file and self._local_repo.ready:
            if not additional_fix_snippet or not await self._snippet_is_grounded(
                additional_fix_file, additional_fix_snippet
            ):
                problems.append(
                    f"additional_fix_file '{additional_fix_file}' has no verbatim-matching "
                    f"additional_fix_snippet. Read the file to confirm it, or remove this field."
                )

        additional_fix_targets = _parse_file_entries(data.get("additional_fix_targets"))
        if self._local_repo.ready:
            for i, target in enumerate(additional_fix_targets):
                snippet = target.get("snippet", "")
                if not snippet or not await self._snippet_is_grounded(target["file"], snippet):
                    problems.append(
                        f"additional_fix_targets[{i}] ({target['file']}) has no verbatim-matching "
                        f"snippet. Read the file to confirm it, or remove this entry."
                    )

        blast_radius = _parse_file_entries(data.get("blast_radius"))
        for i, entry in enumerate(blast_radius):
            snippet = entry.get("snippet", "")
            if snippet and not await self._snippet_is_grounded(entry["file"], snippet):
                problems.append(
                    f"blast_radius[{i}] ({entry['file']}) has a snippet that doesn't match the "
                    f"file's actual current content. Re-read the file, or drop the snippet field."
                )

        # The gap a coarse "is additional_fix_targets completely empty" check missed:
        # once even ONE entry is grounded, a second, third, fabricated file name sitting
        # in the same prose paragraph passed silently. Check every file-like mention
        # individually against the structured entries instead.
        structured_files = {f for f in (affected_file, additional_fix_file) if f}
        structured_files |= {t["file"] for t in additional_fix_targets}
        prose = " ".join(filter(None, [data.get("root_cause", ""), data.get("additional_fix", "")]))
        for mention in sorted(set(_FILE_MENTION_RE.findall(prose))):
            if not any(mention == f or f.endswith("/" + mention) for f in structured_files):
                problems.append(
                    f"'{mention}' is named in root_cause/additional_fix text but has no matching "
                    f"additional_fix_targets entry. Either add a verified entry for it (read the "
                    f"file, include a real snippet), or remove it from the text."
                )

        return problems

    async def _enforce_grounding(
        self, result: DiagnosisResult, incident_tokens: set[str] | None = None
    ) -> DiagnosisResult:
        """Complementary checks that run AFTER a submission already passed
        _validate_diagnosis_submission — everything that check covers
        (function/file existence, pairing, verbatim snippets on affected_file/
        additional_fix_file/additional_fix_targets/blast_radius) is guaranteed
        true by construction at this point, since submit_diagnosis rejected
        anything that failed those checks before result ever existed.

        What's left, and NOT redundant with that gate:
          - Prose scan: the same fabricated names that used to leak into
            root_cause/fix_approach/additional_fix prose can still appear there
            even when every STRUCTURED field is clean -- a symbol mentioned only
            in reasoning, never assigned to any field submit_diagnosis validates.
          - Blast radius coverage: an empty blast_radius that ISN'T an honest
            entry-point case is a completeness signal, not a fabrication one.
        """
        # ----- Prose scan -----------------------------------------------
        # Structured fields are already grounded by submit_diagnosis at this
        # point, but the same fabricated names can still leak into root_cause /
        # fix_approach / additional_fix prose, mentioned only in reasoning and
        # never assigned to a field that gate checks. Verify any function-shaped
        # tokens there too.
        prose = " ".join(filter(None, [
            result.root_cause,
            result.fix_approach,
            result.additional_fix,
        ]))
        candidates = _extract_prose_symbols(prose)

        # Skip names already verified as part of the submission itself.
        already_seen: set[str] = set()
        for n in (result.affected_function, result.additional_fix_function):
            if n:
                already_seen.add(n)
        # Also skip tokens that came directly from the incident error text — they
        # are not LLM inventions, so absence from the repo is expected (e.g.
        # a Mongoose config key that needs to be ADDED, not an existing function).
        excluded = already_seen | (incident_tokens or set())
        to_check = [n for n in candidates if n not in excluded][:_MAX_PROSE_CANDIDATES]

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
        await self._ensure_local_repo()
        # Reset in case this agent instance is reused across diagnose() calls —
        # a stale value from a previous call must never leak into this one.
        self._diagnosis_submitted = None
        self._rejection_count = 0

        event = incident.error_event
        log_group = event.metadata.get("log_group", "")
        pattern = event.metadata.get("pattern", event.error_type or event.title)

        # Tokens extracted from the incident's own error text are not LLM inventions.
        # Exclude them from prose grounding so we don't penalise the diagnosis for
        # citing a library property / config key that needs to be added (e.g. strictQuery).
        incident_text = " ".join(filter(None, [
            event.error_type, event.title, event.description, incident.triage_reasoning,
        ]))
        incident_tokens: set[str] = set(_extract_prose_symbols(incident_text))

        # Deterministic fast path: if the raw error text already names this
        # app's own file(s) directly (the common case for uncaught Node.js
        # exceptions), skip the embedding-search step entirely and tell the
        # model to read them directly. See extract_stack_trace_paths's
        # docstring for why this beats search_codebase for this subset.
        detected_paths = extract_stack_trace_paths(event.description or "")
        if self._local_repo.ready:
            detected_paths = [p for p in detected_paths if self._local_repo.file_exists(p["file"])]
        stack_trace_section = ""
        if detected_paths:
            lines = [
                f"  - {p['file']}" + (f" (function: {p['function']})" if p["function"] else "")
                for p in detected_paths
            ]
            stack_trace_section = (
                "\nSTACK TRACE FILE DETECTION (deterministic, extracted from the error text "
                "and confirmed to exist in the repo — not a search result):\n"
                + "\n".join(lines) + "\n"
                "These are real, confirmed paths. In step 5, call get_file_contents on these "
                "FIRST instead of search_codebase — do not spend a step searching for a file "
                "you already have. Only fall back to search_codebase if none of these turn out "
                "to contain the actual bug.\n"
            )

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
            prior_section = (
                f"\nPRIOR KNOWLEDGE (from past incidents — a LEAD to investigate, not a "
                f"citable fact):\n{prior_context}\n"
                f"This match is keyed on error_type + service + description — not a "
                f"guarantee the past incident is actually the same bug, especially for a "
                f"service that crashes for many unrelated reasons under the same generic "
                f"error_type. Real production bug: a past incident matched this way was for "
                f"a completely different route/file, and the diagnosis cited its (wrong) PR "
                f"as evidence that several sibling files were 'already fixed,' fabricating "
                f"specifics (a file count) that weren't even IN this prior-knowledge text. "
                f"Before citing anything from PRIOR KNOWLEDGE in root_cause: read the actual "
                f"current file(s) yourself and confirm the SAME code shape and symptom match. "
                f"Never state a specific fact (which files were fixed, how many, when) unless "
                f"you verified it by reading real current code — not by inferring it from this "
                f"summary or from a general pattern described elsewhere (e.g. AGENTS.md).\n"
            )

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
{log_group_warning}{prior_section}{stack_trace_section}
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
   If STACK TRACE FILE DETECTION above listed any paths, skip search_codebase for
   this step — call get_file_contents directly on each listed path instead. Only
   fall back to the search below if none of those files turn out to be relevant.

   Otherwise, use specific terms from the stack trace (function names, file paths)
   found in step 1, NOT just the raw error type. If the stack trace shows
   `insertMany appmasterreferrals`, query that. If it shows `classifyFields`,
   query that function name.

   If there is NO stack trace (e.g. a DeprecationWarning, startup warning, or config
   warning), find the call site with an exact search:
     a. Call grep_codebase with the exact string that triggers the warning
        (e.g. "mongoose.connect" for a Mongoose warning). This returns EVERY file
        and line number where the pattern appears — it is exact, not fuzzy.
     b. Call get_file_contents on EACH matching file. Read them all.
     c. Pick the service entry point — the file that is actually executed when the
        process starts (typically server.js, index.js, or the "main" in package.json).
     d. After reading the file, check if the call site is at TOP LEVEL (no enclosing
        function). If so, set affected_function to null — do NOT invent "main" or "run".
        This is the correct answer for module-level scripts and does NOT lower confidence.
   CRITICAL: affected_file MUST be a file path that literally appeared in your
   grep_codebase results. The service name (e.g. an ECS task name) is NOT a
   filename — do not append ".js" to service names. If grep returned
   "create-post-fargate.js" and "server.js", those are your only valid choices.

   If grep_codebase returns matches across multiple files:
     - Put the primary entry point in affected_file (the file most directly causing
       the incident — e.g. the Fargate task file for a scheduled-task warning)
     - After identifying the primary, explicitly check whether server.js, index.js,
       or app.js ALSO contains the same issue (call get_file_contents on each if grep
       returned them). If EXACTLY ONE other file needs the fix, put it in
       additional_fix_file. If TWO OR MORE files need it (e.g. a copy-pasted-per-
       brand/tenant/region duplication convention — check the target's own agent
       guide/notes for this), use additional_fix_targets instead — a list, one
       entry per file: {{"file": ..., "function": ... or null, "snippet": ...}}.
       additional_fix_file can only ever carry ONE file; naming several files in its
       prose description while leaving the structured field singular means the fix
       agent structurally cannot act on any but (at most) one of them.
     - Set additional_fix to a short description of the identical change needed
       (e.g. "Add mongoose.set('strictQuery', true) before mongoose.connect in server.js")
   Do NOT list worker files (routes/workers/**) as the primary or secondary unless
   the incident is specifically about a worker. Focus on top-level entry points.
   The fix agent will commit every file named in additional_fix_file /
   additional_fix_targets in the same PR.

   MANDATORY, for every entry in additional_fix_file/additional_fix_targets, not
   only per-target duplication cases: naming a file requires PROOF, not a name-
   pattern guess or a memory of an earlier incident. Call get_file_contents on that
   EXACT file and copy a real excerpt of its CURRENT vulnerable code into the
   matching snippet field — do NOT reuse or adapt the primary file's snippet with
   names swapped; that is pattern-completion, not reading. This matters most when
   the codebase has a copy-pasted-per-target duplication convention: a file
   matching the naming convention is not automatically still broken. Siblings get
   patched independently by earlier, separate incidents, so a file you "remember"
   being vulnerable may already be fixed — confirm each one individually, don't
   assume the whole family is still broken because one member was. Any entry with
   no snippet, or a snippet that isn't verbatim from that specific file, is
   discarded — not kept as an unverified guess. This applies even if you only
   describe secondary files in additional_fix prose without setting
   additional_fix_file/additional_fix_targets: prose naming specific files as
   "confirmed" still vulnerable with nothing structured and grounded behind it gets
   flagged as unverified too. You gain nothing by guessing instead of reading each
   file — an omitted claim costs nothing; a wrong one wastes a review cycle.

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

   MANDATORY: before naming affected_file, copy a verbatim excerpt of the actual code
   you just read — the specific lines that show the claimed bug — into
   root_cause_snippet. Not a paraphrase, not a reconstruction from memory of what a
   similar file elsewhere looked like: the literal text from THIS file, from a real
   get_file_contents/read_file call in THIS diagnosis. Real production bug: a diagnosis
   named a real file as affected_file and quoted a root_cause code snippet that existed
   nowhere in the actual repo — not in that file, not in any file — while the real bug
   (a different file entirely) was never found. affected_file with no root_cause_snippet,
   or a snippet that isn't verbatim from that file, is discarded — the same policy
   additional_fix_file already has, applied here because affected_file is the field that
   actually gets edited, making it the most consequential one to get right.

7. verify_symbol_in_repo — MANDATORY CODE-GROUNDING STEP (do not skip).
   For EVERY function name you intend to put in affected_function or additional_fix_function,
   call verify_symbol_in_repo with that exact name. This is not optional, even if the name
   "obviously" should exist or "matches the naming convention".

   MODULE-LEVEL EXCEPTION — skip this step entirely when the fix is in module-level code:
   If you read the file in step 6 and the call site you need to fix is at the TOP LEVEL of
   the script (not inside any named function — e.g. `mongoose.connect(...)` sitting directly
   in the file body with no enclosing `function foo()` or arrow function), then:
     - Set affected_function to null. This is the CORRECT answer, not a gap.
     - Do NOT invent a name like "main", "run", "init", or "start" just to have a value.
     - Do NOT call verify_symbol_in_repo for an invented name — skip step 7 entirely.
     - null for module-level code does NOT lower confidence. It is honest and accurate.

   Two valid outcomes for named functions:
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

   Only verify symbols you intend to put in affected_function or additional_fix_function.
   Do NOT verify every function name that appears in a stack trace or as context — only
   the PRIMARY function you are targeting for the fix. Verifying peripheral symbols wastes
   tool calls and incorrectly lowers confidence when they are wrappers or test helpers
   that may not exist on the default branch.

8. Call submit_diagnosis with your findings. It validates every grounding-relevant
   field before accepting — a rejection tells you exactly what's wrong; fix it and
   call submit_diagnosis again. Do NOT write the diagnosis as JSON in your Answer;
   submit_diagnosis is the only way to finalize.

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

After identifying affected_function, find all callers using find_callers
with the function name. This returns the complete caller list in one call.
If find_callers returns no results, fall back to search_codebase. For each
distinct caller you find, add an entry to `blast_radius`:

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

`root_cause` MUST quote route paths, endpoint names, and other identifying
strings VERBATIM from the code you actually read — never paraphrase or
invent a "cleaner-looking" version. Real production bug: a diagnosis wrote
"GET /get-preview/:previewCode route handler" when the real route (visible
in the file it correctly diagnosed and fixed) was "/getPreviewUser/:id" —
a fabricated path that never appeared anywhere in the codebase. The
structured affected_file/affected_function fields were still correctly
grounded, so the actual code change was right, but the self-critique step
(which sees root_cause alongside the real diff) read the fabricated route
name, couldn't find it anywhere near the diff, and flagged a false
"targets the wrong route" failure on an otherwise-correct fix — wasting a
review cycle on a problem that didn't exist. Copy the exact string as it
appears in the file; if you're describing it from memory rather than
something you just read, that's the signal to go re-read the file first.

Call submit_diagnosis with these fields — NOT a JSON Answer:
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
  "root_cause_snippet": "verbatim excerpt of the CURRENT code in affected_file that actually shows the claimed bug — copied from a real get_file_contents/read_file call, not from memory",
  "additional_fix": "optional: describe any secondary change in a different function/file, or null",
  "additional_fix_function": "secondaryFunctionName or null",
  "additional_fix_file": "path/to/secondary/file.js or null",
  "additional_fix_snippet": "verbatim excerpt of the CURRENT vulnerable code you actually read in additional_fix_file, or null",
  "additional_fix_targets": [
    {{"file": "path/to/sibling.js", "function": "handlerName or null", "snippet": "verbatim excerpt of the CURRENT vulnerable code you actually read in THIS file"}}
  ],
  "reproduction_confirmed": true,
  "blast_radius": [
    {{"file": "path/to/caller.js", "function": "callerFunction", "snippet": "const x = primaryFunctionToFix(...)"}}
  ],
  "contract_change": "none",
  "contract_change_detail": null
}}
submit_diagnosis will reject anything ungrounded and tell you exactly what's wrong —
fix it and call it again. Only after it returns "Diagnosis accepted" should you write
a final Answer to end the turn.

Confidence guide:
  0.90+ → near certain, clear evidence in code + logs, AND affected_function verified FOUND
  0.80-0.90 → direct code observation (saw the exact line) AND matching stack traces, even if peripheral symbols unverified
  0.70-0.80 → probable, strong log evidence but limited code visibility
  0.50-0.70 → possible, pattern matches but incomplete evidence
  <0.50 → uncertain, escalate to human
  If log_group was missing and steps 1–3 returned no data, cap confidence at 0.75.
  If affected_function or additional_fix_function returned NOT_FOUND in step 7, cap confidence
  at 0.65 and null those fields. Do NOT lower confidence for symbols mentioned only in evidence
  prose — those are context references, not the fix target.
  If affected_function is null because the fix is at MODULE LEVEL (no enclosing function exists),
  this does NOT lower confidence — null is the correct answer for top-level script code."""

        await self.run(prompt)

        if self._diagnosis_submitted is not None:
            submitted = self._diagnosis_submitted
            self._diagnosis_submitted = None  # don't leak into a future call on this instance
            return await self._enforce_grounding(submitted, incident_tokens)

        # The model never got a submission through submit_diagnosis before exhausting
        # its iteration budget — same fail-closed policy as BaseAgent.run()'s own
        # "never verified any claim" fallback: don't trust free-text content that was
        # never checked. There is no free-text JSON to fall back to parsing anymore —
        # that's the point; a diagnosis that never passed the gate gets no structured
        # fields at all, not fields nulled after the fact.
        logger.warning(
            "DiagnosisAgent: never received a successful submit_diagnosis call for incident %s "
            "— degrading to escalate.",
            incident.id,
        )
        return DiagnosisResult(
            root_cause="Diagnosis could not be grounded — manual review required",
            confidence=0.0,
            escalate=True,
            grounding_rejections=self._rejection_count,
        )
