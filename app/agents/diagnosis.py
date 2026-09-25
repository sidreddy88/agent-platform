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

After submit_diagnosis accepts: _enforce_grounding re-checks prose for
fabricated symbol names, then _apply_output_validation (see
app.services.output_validator) checks a different failure class — citations
to real files/functions the agent never actually retrieved this run, and
leaked internal context-wrapping markers (a sign the model echoed
ipi_guard-wrapped untrusted content back into its own answer instead of
synthesizing one). Either failure forces escalate=True for human review;
neither retries in-loop like the grounding checks above.

Confidence gate (CONFIDENCE_THRESHOLD = 0.70):
  ≥ 0.70 → status = FIXING (proceed to Fix Generation Agent — Week 3)
  < 0.70 → status = AWAITING_APPROVAL (human escalation via Slack)
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.agents.base import BaseAgent
from app.agents.harness import Harness, load_harness
from app.core.config import settings
from app.models.events import IncidentState
from app.services.aws import AWSError, AWSService
from app.services.github import GitHubService
from app.services.ipi_guard import scan_for_injection, wrap_untrusted
from app.services.llm import LLMService
from app.services.output_validator import validate_diagnosis_output
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

# Per-read cap on get_file_contents output, whole-file or ranged. The grounding
# gate uses it too: when a snippet fails to match, whether the file is bigger
# than one read decides what advice can actually work. See _snippet_problem().
# The live value is the harness setting "file_read_char_limit" (an agent can
# run a candidate harness with a different one); this is the default harness's
# value, for callers with no agent at hand.
_FILE_READ_CHAR_LIMIT = load_harness(
    "diagnosis", Path(__file__).resolve().parent / "harness" / "diagnosis"
).setting("file_read_char_limit")


def _cap_lines(lines: list[str], first: int, total: int, file_path: str,
               limit: int = _FILE_READ_CHAR_LIMIT) -> str:
    """Join lines (numbered from `first`) up to `limit` chars, cutting
    only at a line boundary, and say exactly which lines were returned.

    The old notice said "only first 12000 chars shown" and suggested
    re-reading "with a more specific path" -- meaningless for one file, and
    re-reading returned the identical prefix. Line numbers make the rest of
    the file addressable.
    """
    out: list[str] = []
    size = 0
    for line in lines:
        if out and size + len(line) > limit:
            break
        out.append(line)
        size += len(line)
    last = first + len(out) - 1
    text = "".join(out)
    if first == 1 and last == total:
        return text
    notice = f"[Showing lines {first}-{last} of {total} in {file_path}."
    if last < total:
        notice += (f" To read further, call get_file_contents with start_line/end_line"
                   f" (e.g. start_line={last + 1}); grep_codebase finds line numbers.")
    return text.rstrip("\n") + "\n\n" + notice + "]"


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

def _submission_fingerprint(kwargs: dict) -> str:
    """Stable hash of the fields the grounding gate actually checks.

    Used only to notice that the model has re-sent something already rejected.
    Deliberately narrow — confidence wobbling by 0.05 between attempts is not a
    change of substance, and shouldn't hide a genuine repeat.
    """
    keyed = {
        k: kwargs.get(k) for k in (
            "affected_file", "affected_function", "root_cause_snippet",
            "additional_fix_file", "additional_fix_function", "additional_fix_snippet",
            "root_cause",
        )
    }
    return hashlib.sha256(
        json.dumps(keyed, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

# CPython traceback frame:  File "/testbed/django/db/models/query.py", line 71, in __iter__
# Note the inverted order vs V8 — the path comes first, the function name last
# (and is optional; the trailing ", in <name>" is absent for module-level frames).
_PY_FRAME_RE = re.compile(
    r'File "([^"\n]+\.py)", line \d+(?:, in ([A-Za-z_]\w*))?'
)

# Python's equivalent of node_modules: installed dependencies and stdlib. A
# frame in one of these is library code, not the repo under diagnosis.
_PY_VENDOR_MARKERS = (
    "/site-packages/", "/dist-packages/", "/lib/python", "/.venv/", "/venv/",
    "<frozen ", "/usr/lib/", "/opt/conda/",
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

    # ── CPython tracebacks ────────────────────────────────────────────────
    # Paths here are absolute against whatever root the process ran under
    # (/testbed/... under SWE-bench, /srv/app/... in a container), so unlike
    # the V8 branch there's no single prefix to strip. Emit progressively
    # shorter suffixes and let the caller's repo file_exists() check pick the
    # one that resolves — the caller already filters, so extra candidates cost
    # nothing and a wrong guess here can't leak through.
    for m in _PY_FRAME_RE.finditer(text):
        raw_path, fn = m.group(1), m.group(2)
        if any(marker in raw_path for marker in _PY_VENDOR_MARKERS):
            continue
        parts = raw_path.lstrip("/").split("/")
        # Longest first: "django/db/models/query.py" before "db/models/query.py".
        # Stop at two segments — a bare "query.py" or "base.py" resolves against
        # half a dozen unrelated packages in repos this size, and file_exists()
        # would happily confirm the wrong one.
        for i in range(max(len(parts) - 1, 1)):
            candidate = "/".join(parts[i:])
            if candidate not in seen or (seen[candidate] is None and fn is not None):
                seen[candidate] = fn

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

    @property
    def _harness(self) -> Harness:
        """The loaded harness. Falls back to the default one when __init__ never
        ran (tests build agents with DiagnosisAgent.__new__ and wire only what
        they need), so every tool closure can rely on it."""
        harness = self.__dict__.get("_harness_loaded")
        if harness is None:
            harness = self.__dict__["_harness_loaded"] = load_harness("diagnosis")
        return harness

    @_harness.setter
    def _harness(self, value: Harness) -> None:
        self.__dict__["_harness_loaded"] = value


    def __init__(
        self,
        aws: AWSService | None = None,
        rag: RAGService | None = None,
        github: GitHubService | None = None,
        local_repo: LocalRepoService | None = None,
        owner: str | None = None,
        repo: str | None = None,
        harness_dir: str | Path | None = None,
        code_graph: CodeGraph | None = None,
    ) -> None:
        super().__init__(llm=LLMService())   # Sonnet — default model
        # Prompt text, tool descriptions and settings live in a harness
        # directory (app/agents/harness/diagnosis/ by default), not in string
        # literals here: that directory is the harness optimizer's edit
        # surface. harness_dir / HARNESS_DIR_DIAGNOSIS point at a candidate.
        self._harness = load_harness("diagnosis", harness_dir)
        self._aws = aws or AWSService()
        self._rag = rag
        self._github = github or GitHubService()
        if owner and repo:
            # Overridable so cross-repo eval tooling (scripts/eval_swebench_diagnosis.py)
            # can point diagnosis at an arbitrary GitHub repo instead of the one
            # fixed target app -- settings.fix_target_repo has no notion of "a
            # different repo per benchmark instance".
            self._owner, self._repo = owner, repo
        else:
            self._owner, self._repo = settings.fix_target_repo.split("/", 1)
        # Overridable so replay/eval tooling (scripts/eval_diagnosis_regression.py,
        # scripts/eval_swebench_diagnosis.py) can pin diagnosis to an isolated
        # historical worktree instead of the live shared clone's current HEAD —
        # see LocalRepoService(pinned_sha=...).
        self._local_repo = local_repo or LocalRepoService(self._owner, self._repo)
        # The call graph find_callers answers from. The module-level
        # _code_graph is the TARGET APP's graph (loaded from the store at
        # import); it is only right when diagnosing the target app. Every
        # SWE-bench replay used to query it anyway, so find_callers searched
        # a JS app's graph while diagnosing Python repos. None here means
        # "resolve per repo in diagnose()" (_ensure_code_graph).
        self._code_graph = code_graph
        self._register_tools()
        # Raised from BaseAgent's default of 10 -- a turn-by-turn trace + SWE-bench
        # spot checks found the correction-cycling phase (after the grounding gate
        # first rejects a submission) regularly needs more than 10 rounds on
        # unfamiliar code: 3 of 4 previously-failing instances recovered when
        # raised to 15 (pytest-10051 needed 11, astropy-13236 and sympy-11618 used
        # the full 15; django-10554 still failed even at 15 -- its real fix spans
        # 2 files, a different problem more turns alone doesn't solve). Scoped to
        # this agent only: no evidence yet that any other agent needs more room.
        self._max_iterations = self._harness.setting("max_iterations")
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
        # Fingerprint of the last rejected submission + its reasons, so a verbatim
        # re-send can be called out instead of answered with the same message.
        self._last_rejection_signature: tuple | None = None
        # Raw text of every chunk search_codebase actually returned during one
        # diagnose() call -- read externally (incident_loop.py) as the "retrieved
        # context" input to the sampled RAG faithfulness judge. Reset at the top
        # of every diagnose() call, same as the two attributes above.
        self._last_retrieved_chunks: list[str] = []
        # Every file path this diagnose() call actually touched -- via
        # search_codebase hits, get_file_contents fetches, grep_codebase matches,
        # verify_symbol_in_repo hits, and find_callers results. Feeds
        # output_validator.validate_diagnosis_output's citation-provenance check:
        # a citation can be real (passes grounding) and still never have been
        # looked at this run. Reset at the top of every diagnose() call, same as
        # the attributes above.
        self._retrieved_file_paths: set[str] = set()

    @property
    def last_retrieved_chunks(self) -> list[str]:
        return list(self._last_retrieved_chunks)

    @property
    def retrieved_file_paths(self) -> set[str]:
        return set(self._retrieved_file_paths)

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
            self._last_retrieved_chunks.extend(parts)
            self._retrieved_file_paths.update(c.file_path for c in chunks)
            raw = "\n\n".join(parts)
            scan_for_injection(raw, source="rag-codebase-search")
            return wrap_untrusted(raw, source="rag-codebase-search")

        async def _get_file_contents(file_path: str, start_line: int | None = None,
                                     end_line: int | None = None) -> str:
            """Fetch a file's source from the target repo, whole or as a line range.

            Prefers the local repo clone whenever it's ready — covers both
            pinned replay/eval (the pre-fix historical SHA) and live diagnosis
            (a clone of the repo's actual current state). This sidesteps
            get_file_contents's hardcoded ref="main" (github.py), which 404s
            against any repo whose real default branch isn't main — confirmed
            live against the target app, whose default branch is master, where
            every get_file_contents call 404'd for an entire run while
            grep_codebase (which always reads the local clone, pinned or not)
            kept working against the same file. Falls back to the GitHub API
            only if the local read fails or no clone is ready. Same bug class
            as _symbol_exists_in_repo's pinned-mode fix, widened to live mode.

            Why a line range: whole-file reads are capped at _FILE_READ_CHAR_LIMIT,
            and nothing past the cap was reachable. On matplotlib__matplotlib-22865
            the bug sat ~line 650 of a colorbar.py far past the cut; the agent never
            saw it, invented a method name, and burned its budget on grounding
            rejections asking for an excerpt it could not read. grep_codebase gives
            line numbers; a range read then returns that region verbatim, so the
            snippet it copies matches the file.
            """
            try:
                p = file_path.lstrip("/")
                content = None
                if self._local_repo.ready:
                    try:
                        content = self._local_repo.read_file(p)
                    except Exception:
                        content = None
                if content is None:
                    content, _ = await github.get_file_contents(owner, repo, p)
                read_limit = self._harness.setting("file_read_char_limit")
                lines = content.splitlines(keepends=True)
                total = len(lines)
                if start_line is not None or end_line is not None:
                    first = max(1, int(start_line or 1))
                    last = min(total, int(end_line or total))
                    if first > last:
                        return (f"Invalid range {start_line}-{end_line} for {file_path} "
                                f"({total} lines).")
                    content = _cap_lines(lines[first - 1:last], first, total, file_path, read_limit)
                elif len(content) > read_limit:
                    content = _cap_lines(lines, 1, total, file_path, read_limit)
                self._retrieved_file_paths.add(p)
                scan_for_injection(content, source=f"github-file:{file_path}")
                return wrap_untrusted(content, source=f"github-file:{file_path}")
            except Exception as exc:
                return f"Could not fetch {file_path}: {exc}"

        async def _grep_codebase(pattern: str, file_glob: str | None = None) -> str:
            """Exact-string search across every file in the local repo clone.

            Returns each matching line with its file path and line number.
            Use this when you need to find WHERE a specific function or string is
            called/defined — e.g. 'mongoose.connect' or 'require(\"mongoose\")'.
            Faster and more precise than search_codebase for exact patterns.
            Input: {pattern: string, file_glob: string (default '*', every file)}

            The default was '*.js', a leftover from the JS-only target app: on
            every Python repo an unscoped grep searched nothing and reported
            "No matches", which reads as evidence the code doesn't exist.
            """
            local_repo = self._local_repo
            if not local_repo.ready:
                return "Local repo not available — use search_codebase instead."
            import fnmatch
            file_glob = file_glob or self._harness.setting("grep_default_glob")
            max_matches = self._harness.setting("grep_max_matches")
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
                            self._retrieved_file_paths.add(rel_path)
                            if len(matches) >= max_matches:
                                break
                    if len(matches) >= max_matches:
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
                        self._retrieved_file_paths.add(h["path"])
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
            self._harness.tool_description("get_error_samples"),
        )
        self.register_tool(
            "check_still_occurring",
            _check_still_occurring,
            self._harness.tool_description("check_still_occurring"),
        )
        self.register_tool(
            "get_occurrence_timeline",
            _get_occurrence_timeline,
            self._harness.tool_description("get_occurrence_timeline"),
        )
        self.register_tool(
            "search_codebase",
            _search_codebase,
            self._harness.tool_description("search_codebase"),
        )
        self.register_tool(
            "get_file_contents",
            _get_file_contents,
            self._harness.tool_description("get_file_contents"),
        )
        self.register_tool(
            "grep_codebase",
            _grep_codebase,
            self._harness.tool_description("grep_codebase"),
        )
        self.register_tool(
            "search_similar_incidents",
            _search_similar_incidents,
            self._harness.tool_description("search_similar_incidents"),
        )
        self.register_tool(
            "verify_symbol_in_repo",
            _verify_symbol_in_repo,
            self._harness.tool_description("verify_symbol_in_repo"),
        )

        async def _find_callers(function_name: str) -> str:
            """Look up every caller of a function in the call graph index."""
            graph = getattr(self, "_code_graph", None) or _code_graph
            callers = graph.find_callers(function_name)
            if not callers:
                return (
                    f"No callers found for '{function_name}' in the call graph index. "
                    f"Either it is an entry point (route/handler/cron) or the index has not been built. "
                    f"Fall back to search_codebase to find callers manually."
                )
            lines = [f"Callers of `{function_name}` ({len(callers)} found):"]
            for c in callers[:15]:
                lines.append(f"  {c.file_path} → {c.function_name} (line {c.line})")
                self._retrieved_file_paths.add(c.file_path)
            if len(callers) > 15:
                lines.append(f"  ... and {len(callers) - 15} more")
            return "\n".join(lines)

        self.register_tool(
            "find_callers",
            _find_callers,
            self._harness.tool_description("find_callers"),
        )

        async def _submit_diagnosis(**kwargs) -> str:
            problems = await self._validate_diagnosis_submission(kwargs)
            if problems:
                self._rejection_count += 1
                logger.info(
                    "DiagnosisAgent: submit_diagnosis rejected (attempt %d): %s",
                    self._rejection_count, "; ".join(problems),
                )
                # Loop breaker. A real trajectory (psf__requests-1142) rejected
                # four times on byte-identical grounds against a byte-identical
                # submission, each round re-reading the same file, until the
                # 15-iteration budget ran out and the run escalated. Repeating
                # the same rejection verbatim is not feedback — it reads as
                # "try again" when the honest content is "this exact thing has
                # already failed". Say that instead.
                signature = (tuple(problems), _submission_fingerprint(kwargs))
                repeated = signature == self._last_rejection_signature
                self._last_rejection_signature = signature
                header = "REJECTED — fix the following and call submit_diagnosis again:"
                if repeated:
                    header = (
                        "REJECTED AGAIN — this is the SAME submission you just sent, failing "
                        "for the SAME reasons. Re-sending it will fail identically.\n"
                        "Stop and change your approach: if a snippet won't verify, the text you "
                        "are submitting is not in the file. Do not reconstruct it from memory — "
                        "locate the real text with grep_codebase, or set the field to null and "
                        "lower your confidence. Outstanding problems:"
                    )
                return header + "\n" + "\n".join(f"- {p}" for p in problems)
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
            self._harness.tool_description("submit_diagnosis"),
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

    def _snippet_problem(
        self, field: str, file_path: str, symbol: str | None, snippet: str | None
    ) -> str:
        """Explain a failed snippet check in a way the model can act on.

        The old message was a single string for every case: "call
        get_file_contents on X and copy a real excerpt". A real trajectory
        (psf__requests-1142) showed why that can be actively wrong.
        requests/models.py is 20,789 chars; get_file_contents returns the first
        12,000; the target function's *definition* sat past the cut, so only
        its call site was visible. The model reconstructed the body from
        memory — plausible, well-formed, not the file's actual text — and was
        rejected. It then did exactly what the message said, re-read the file,
        got the identical truncated content, and resubmitted the identical
        fabrication. Four rounds, budget exhausted, run escalated.

        The instruction was followed faithfully and could not have worked. So
        the message now distinguishes the cases, and when the file is larger
        than the read limit it names the one tool that *can* reach the rest.
        """
        missing = not snippet
        head = (
            f"{field} is missing"
            if missing else
            f"{field} does not match {file_path}'s actual current content "
            f"(the text you submitted is not in the file)"
        )

        size = None
        try:
            if self._local_repo.ready:
                size = len(self._local_repo.read_file(file_path))
        except Exception:
            size = None

        read_limit = self._harness.setting("file_read_char_limit")
        if size is not None and size > read_limit:
            target = f"'{symbol}'" if symbol else "the relevant code"
            return (
                f"{head}. NOTE: {file_path} is {size} chars and a plain get_file_contents "
                f"call returns only the first {read_limit} — re-reading it the "
                f"same way returns the same partial content. Use grep_codebase to find the "
                f"line number of {target}, then call get_file_contents with start_line/"
                f"end_line around it and copy the snippet verbatim from that read. Do not "
                f"reconstruct the code from memory; if you cannot retrieve the real text, "
                f"set {field} to null and lower confidence."
            )

        return (
            f"{head}. Call get_file_contents on {file_path} and copy a verbatim excerpt "
            f"showing the claimed bug — not a paraphrase or a remembered version. If the "
            f"code isn't there, set {field} to null and lower confidence rather than "
            f"inventing one."
        )

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
                problems.append(self._snippet_problem(
                    field="root_cause_snippet",
                    file_path=affected_file,
                    symbol=affected_function,
                    snippet=root_cause_snippet,
                ))

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

    def _apply_output_validation(self, result: DiagnosisResult, incident: IncidentState) -> None:
        """Security-oriented output check — see app.services.output_validator.

        Distinct from _enforce_grounding: that asks "is this cited content
        real?"; this asks "did the agent actually retrieve it this run, and
        does the output leak internal context-wrapping markers?" On failure,
        forces escalate=True so a human reviews before FixGenerationAgent
        ever sees this diagnosis — the same routing a low-confidence
        diagnosis already gets, just a different trigger. Never raises: a
        validator bug must not crash a diagnosis that would otherwise have
        shipped fine.
        """
        try:
            validation = validate_diagnosis_output(result, self._retrieved_file_paths)
        except Exception:
            logger.exception(
                "DiagnosisAgent: output_validator crashed for incident %s — skipping", incident.id
            )
            return
        if not validation.passed:
            logger.warning(
                "DiagnosisAgent: output validation failed for incident %s — %s",
                incident.id, "; ".join(validation.failures),
            )
            result.evidence = [
                *result.evidence,
                f"OUTPUT VALIDATION WARNING: {'; '.join(validation.failures)}",
            ]
            result.escalate = True

    def _is_target_repo(self) -> bool:
        target = (settings.fix_target_repo or "").split("/", 1)
        return len(target) == 2 and (self._owner, self._repo) == (target[0], target[1])

    async def _ensure_code_graph(self) -> None:
        """Make sure find_callers answers from THIS repo's call graph.

        Target app: the stored graph (built by scripts/index_code_graph.py).
        Any other repo (eval replays of SWE-bench, cross-repo tooling): build
        from the local checkout, which for a pinned replay is the worktree at
        the instance's base commit. Built once per agent, in a thread (parsing
        a large repo takes seconds of CPU). With no checkout, an empty graph
        rather than another repo's: "no callers" beats wrong callers.
        """
        if getattr(self, "_code_graph", None) is not None:
            return
        if self._is_target_repo():
            self._code_graph = _code_graph
            return
        if self._local_repo.ready:
            self._code_graph = await asyncio.to_thread(
                CodeGraph.build_from_directory, str(self._local_repo.local_path))
            logger.info("DiagnosisAgent: built call graph for %s/%s: %s",
                        self._owner, self._repo, self._code_graph.stats())
            return
        logger.warning("DiagnosisAgent: no local checkout of %s/%s — find_callers has no "
                       "call graph for it (not falling back to the target app's)",
                       self._owner, self._repo)
        self._code_graph = CodeGraph()

    async def diagnose(self, incident: IncidentState, prior_context: str | None = None) -> DiagnosisResult:
        """Run diagnosis on a triaged incident. Returns a DiagnosisResult."""
        await self._ensure_local_repo()
        await self._ensure_code_graph()
        # Reset in case this agent instance is reused across diagnose() calls —
        # a stale value from a previous call must never leak into this one.
        self._diagnosis_submitted = None
        self._rejection_count = 0
        self._last_rejection_signature = None
        self._last_retrieved_chunks = []
        self._retrieved_file_paths = set()

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
            # Python frames emit several suffix candidates per real file
            # ("django/db/models/query.py", "db/models/query.py", ...) and more
            # than one can resolve. Keep the longest surviving path per
            # basename — it's the most specific, so the least likely to be a
            # same-named file in an unrelated package.
            by_basename: dict[str, dict] = {}
            for p in detected_paths:
                base = p["file"].rsplit("/", 1)[-1]
                incumbent = by_basename.get(base)
                if incumbent is None or p["file"].count("/") > incumbent["file"].count("/"):
                    by_basename[base] = p
            detected_paths = list(by_basename.values())
        stack_trace_section = ""
        if detected_paths:
            lines = [
                f"  - {p['file']}" + (f" (function: {p['function']})" if p["function"] else "")
                for p in detected_paths
            ]
            stack_trace_section = self._harness.render("stack_trace", paths="\n".join(lines))

        # Steps 1-3 (get_error_samples, check_still_occurring, get_occurrence_timeline)
        # take arguments fully determined by the incident before the model ever sees a
        # prompt -- log_group/pattern come from event.metadata, minutes/hours are fixed
        # literals. No model judgment was ever involved in deciding these three calls;
        # the model was only ever asked to copy values it was already handed, at the
        # cost of 3 full ReAct-loop round trips (Sonnet calls) on every single
        # diagnosis, before any real investigation even started. Same anti-pattern
        # found and fixed in TriageAgent, confirmed here via
        # scripts/audit_deterministic_tool_calls.py. Fetched directly here instead;
        # their real output is embedded below as pre-loaded context (still labelled
        # "steps 1-3" so every other "step 5"/"step 6"/"step 7" cross-reference
        # elsewhere in this prompt stays correct without renumbering).
        if log_group:
            get_error_samples_fn, _ = self._tools["get_error_samples"]
            check_still_occurring_fn, _ = self._tools["check_still_occurring"]
            get_occurrence_timeline_fn, _ = self._tools["get_occurrence_timeline"]
            error_samples = await get_error_samples_fn(log_group=log_group, pattern=pattern, minutes=120)
            still_occurring = await check_still_occurring_fn(log_group=log_group, pattern=pattern)
            occurrence_timeline = await get_occurrence_timeline_fn(log_group=log_group, pattern=pattern, hours=24)
            log_group_warning = self._harness.render(
                "log_context_fetched",
                error_samples=error_samples,
                still_occurring=still_occurring,
                occurrence_timeline=occurrence_timeline,
            )
        else:
            logger.warning("DiagnosisAgent: log_group missing from incident metadata — log-based steps will produce no results")
            log_group_warning = self._harness.render("log_context_missing")

        prior_section = ""
        if prior_context:
            prior_section = self._harness.render("prior_knowledge", prior_context=prior_context)

        prompt = self._harness.render(
            "task_prompt",
            error_type=event.error_type,
            title=event.title,
            description=event.description,
            service=event.service,
            log_group=log_group or '(not provided)',
            pattern=pattern,
            task_id=event.task_id or '(not provided)',
            severity=event.severity,
            occurrences_24h=incident.occurrences_24h,
            blast_radius=incident.blast_radius,
            triage_reasoning=incident.triage_reasoning,
            log_context=log_group_warning,
            prior_knowledge=prior_section,
            stack_trace=stack_trace_section,
        )

        await self.run(prompt)

        if self._diagnosis_submitted is not None:
            submitted = self._diagnosis_submitted
            self._diagnosis_submitted = None  # don't leak into a future call on this instance
            grounded = await self._enforce_grounding(submitted, incident_tokens)
            self._apply_output_validation(grounded, incident)
            return grounded

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
