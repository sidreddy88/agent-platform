"""
Output validation for DiagnosisAgent — a security-oriented check, distinct
from diagnosis.py's `_enforce_grounding`.

`_enforce_grounding` (and the inline `submit_diagnosis` checks it complements)
answer "does this cited file/function/snippet actually exist in the repo?".
This module answers a different question: "was this content actually part of
what THIS diagnosis run retrieved or read, and does the output show signs the
model echoed its own internal context-wrapping back out instead of
synthesizing an answer?"

Both failure modes are real and distinct:
  - A citation can be 100% real (passes grounding) and still be unprovenanced
    — the model naming a file it never actually looked at this run, because
    the name happened to be plausible, not because it was investigated.
  - `app.services.ipi_guard.wrap_untrusted` labels every piece of untrusted
    external content (logs, GitHub files, RAG chunks) the agent reads with
    structural markers telling the model "this is data, not instructions."
    Those markers showing up in the model's own *answer* is a strong signal
    something went wrong — either a raw dump of retrieved content instead of
    a synthesized diagnosis, or an injected instruction that got the model to
    echo internal structure back out.

Not a blocking retry gate like `submit_diagnosis`'s grounding checks — this
runs once, after a diagnosis is otherwise complete and already grounded. On
failure the caller forces `escalate=True`, routing to human review the same
way a low-confidence diagnosis already does (see `incident_loop.py`'s
`if diagnosis.escalate:` handling) — it does not retry in-loop, because
unlike a fabricated symbol name, there's no well-defined "fix and resubmit"
instruction to hand the model for "this wasn't part of what you retrieved."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.agents.diagnosis import DiagnosisResult

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
    prose = " ".join(filter(None, [
        result.root_cause,
        result.fix_approach,
        result.additional_fix,
        result.root_cause_snippet,
        result.additional_fix_snippet,
    ]))
    for marker in _LEAKED_MARKERS:
        if marker in prose:
            failures.append(
                f"output contains internal context-wrapping marker {marker!r} — "
                "possible raw content dump or injection echo"
            )

    # --- Length anomaly ------------------------------------------------------
    for field_name in ("root_cause", "fix_approach", "additional_fix"):
        value = getattr(result, field_name) or ""
        if len(value) > MAX_FIELD_CHARS:
            failures.append(
                f"'{field_name}' is {len(value)} chars (> {MAX_FIELD_CHARS}) — "
                "abnormally long for a synthesized answer, possible data dump"
            )

    return ValidationResult(passed=not failures, failures=failures)
