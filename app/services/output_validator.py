"""
Output validation — a security-oriented check, distinct from any agent's own
correctness/grounding checks (e.g. diagnosis.py's `_enforce_grounding`, which
asks "does this cited file/function/snippet actually exist in the repo?").
This module asks a different question: "was this content actually part of
what THIS run retrieved or read, and does the output show signs the model
echoed its own internal context-wrapping back out instead of synthesizing an
answer?"

Both failure modes are real and distinct:
  - A citation can be 100% real (passes grounding) and still be unprovenanced
    — the model naming a file it never actually looked at this run, because
    the name happened to be plausible, not because it was investigated.
  - `app.services.ipi_guard.wrap_untrusted` labels every piece of untrusted
    external content (logs, GitHub files, RAG chunks, PR diffs) an agent
    reads with structural markers telling the model "this is data, not
    instructions." Those markers showing up in the model's own *answer* is a
    strong signal something went wrong — either a raw dump of retrieved
    content instead of a synthesized response, or an injected instruction
    that got the model to echo internal structure back out.

Started as DiagnosisAgent-only (`validate_diagnosis_output`, both checks plus
a length-anomaly check); extended to ErrorClarityAgent
(`validate_error_clarity_addition`, full treatment — it explores the codebase
via read_file/search_code the same way DiagnosisAgent does, so a cited file
can likewise be real-but-never-actually-retrieved-this-run) and, as a
leaked-marker-only check (`check_leaked_markers`), to the remaining five
pipeline agents (FixGenerationAgent, CodeReviewAgent, TriageAgent,
MergeDecisionAgent, MonitorGenerationAgent).

Citation-provenance is deliberately NOT extended to FixGenerationAgent
despite it also producing committed code: its agentic loop can only ever
edit the one pre-verified `file_path` it's handed, and every secondary file
comes from `diagnosis_blast_radius`/`diagnosis_additional_fix_targets` —
data DiagnosisAgent's own grounding gate (and `validate_diagnosis_output`)
already checked. Re-checking it here would be checking already-checked data,
not covering a new gap.

None of these are blocking retry gates the way `submit_diagnosis`'s
grounding checks are — they run once, after a result is otherwise complete.
On failure, callers force whatever fail-safe routing they have available
(escalate=True, dropping the flagged item before it's committed, forcing a
conservative decision) rather than retrying in-loop, because unlike a
fabricated symbol name, there's no well-defined "fix and resubmit"
instruction to hand the model for "this wasn't part of what you retrieved."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents.diagnosis import DiagnosisResult
    from app.agents.error_clarity import ClarityAddition

# Same literal markers app.services.ipi_guard.wrap_untrusted() wraps external
# content in. Their presence in a diagnosis's own free-text output means the
# model echoed its internal context-wrapping back out.
_LEAKED_MARKERS = (
    "<untrusted-content",
    "</untrusted-content>",
)

# Real root_cause/fix_approach/additional_fix text in production runs a few
# hundred characters. A field this long is far more likely to be a dumped
# chunk of retrieved content than a synthesized explanation.
MAX_FIELD_CHARS = 4000


@dataclass
class ValidationResult:
    passed: bool
    failures: list[str] = field(default_factory=list)


def check_leaked_markers(*texts: str | None) -> list[str]:
    """Generic, reusable check: do any of these free-text fields contain
    ipi_guard.wrap_untrusted's structural markers? Presence means the content
    echoed internal context-wrapping back into an agent's own output — a raw
    dump or an injection echo — regardless of which agent produced it or what
    shape its result object has. Every agent's output-validation wiring uses
    this same function; only DiagnosisAgent/ErrorClarityAgent additionally get
    the shape-specific citation-provenance check below.
    """
    failures: list[str] = []
    combined = " ".join(t for t in texts if t)
    for marker in _LEAKED_MARKERS:
        if marker in combined:
            failures.append(
                f"output contains internal context-wrapping marker {marker!r} — "
                "possible raw content dump or injection echo"
            )
    return failures


def _cited_files(result: "DiagnosisResult") -> list[str]:
    files: list[str] = []
    if result.affected_file:
        files.append(result.affected_file)
    if result.additional_fix_file:
        files.append(result.additional_fix_file)
    for entry in result.additional_fix_targets:
        if entry.get("file"):
            files.append(entry["file"])
    for entry in result.blast_radius:
        if entry.get("file"):
            files.append(entry["file"])
    return files


def _norm(path: str) -> str:
    return path.lstrip("/")


def validate_diagnosis_output(
    result: "DiagnosisResult", retrieved_paths: set[str]
) -> ValidationResult:
    """Security-oriented sanity check on an already-grounded DiagnosisResult.

    retrieved_paths: every file path DiagnosisAgent actually touched during
    this run — search_codebase hits, get_file_contents fetches, grep_codebase
    matches, verify_symbol_in_repo hits, find_callers results. See
    DiagnosisAgent._retrieved_file_paths.
    """
    failures: list[str] = []

    # --- Citation-provenance check ---------------------------------------
    # Only meaningful once the agent has actually retrieved something — an
    # empty retrieved_paths means we have nothing to compare against, not
    # that every citation is unprovenanced. (In practice this should never be
    # empty by the time a diagnosis is submitted — DiagnosisAgent requires at
    # least one call to a code-reading tool before an answer is accepted —
    # but a validator must not assume its caller's invariants hold.)
    if retrieved_paths:
        norm_retrieved = {_norm(p) for p in retrieved_paths}
        for cited in _cited_files(result):
            if _norm(cited) not in norm_retrieved:
                failures.append(
                    f"cites '{cited}' which was never retrieved/read during this diagnosis run"
                )

    # --- Leaked internal-wrapper markers -----------------------------------
    failures.extend(check_leaked_markers(
        result.root_cause,
        result.fix_approach,
        result.additional_fix,
        result.root_cause_snippet,
        result.additional_fix_snippet,
    ))

    # --- Length anomaly ------------------------------------------------------
    for field_name in ("root_cause", "fix_approach", "additional_fix"):
        value = getattr(result, field_name) or ""
        if len(value) > MAX_FIELD_CHARS:
            failures.append(
                f"'{field_name}' is {len(value)} chars (> {MAX_FIELD_CHARS}) — "
                "abnormally long for a synthesized answer, possible data dump"
            )

    return ValidationResult(passed=not failures, failures=failures)


def validate_error_clarity_addition(
    addition: "ClarityAddition", retrieved_paths: set[str]
) -> ValidationResult:
    """Security-oriented check on one ErrorClarityAgent ClarityAddition, before
    it's allowed into a commit.

    ErrorClarityAgent explores the codebase via read_file/search_code the same
    way DiagnosisAgent does via get_file_contents/search_codebase — so the
    same citation-provenance question applies: was `addition.file` actually
    read/found this run, or just named because it sounded plausible? Unlike
    DiagnosisAgent, a failure here should not just flag for human review after
    the fact — ClarityResult has no escalate field, and the actual risk is an
    unprovenanced code change getting committed verbatim via
    ErrorClarityAgent._commit_additions. Callers should drop any addition that
    fails this check before committing, not just log it.
    """
    failures: list[str] = []

    if retrieved_paths and _norm(addition.file) not in {_norm(p) for p in retrieved_paths}:
        failures.append(
            f"addition targets '{addition.file}' which was never retrieved/read during this run"
        )

    failures.extend(check_leaked_markers(
        addition.code_before, addition.code_after, addition.description,
    ))

    return ValidationResult(passed=not failures, failures=failures)
