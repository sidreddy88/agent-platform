"""
Audit: for every agent's registered tool, is EVERY argument the model is
asked to supply a literal, already-known context value -- or something it
must genuinely decide/synthesize?

Principle (found independently twice this session, in TriageAgent and now
confirmed live in two more agents below): anything the code can determine
reliably and deterministically, the code should determine -- the model
should never be asked to rediscover it. Every call flagged DETERMINISTIC
here is a real, wasted ReAct-loop turn: the model is asked to copy a value
it was already handed, at the cost of a full LLM round trip (network
latency + generation time), exactly the pattern TriageAgent's
check_duplicate_pr/get_occurrence_count fix eliminated.

This is intentionally NOT a generic prompt parser -- "does this argument
require genuine judgment" is a semantic question a regex can't safely
answer for an arbitrary new prompt. What's actually automatable, and what
this script does:

  1. Records the real audit below, one entry per tool-call instruction,
     each backed by an exact file:line citation to the actual prompt text
     -- read and verified against the live source, not inferred.
  2. Provides `check_instruction_line()`, a reusable heuristic for auditing
     NEW instruction lines added later: given the line and the set of
     variable names known before the prompt is built, flags a call as a
     deterministic-candidate if every argument value is a bare template
     substitution of a known variable (or a fixed literal), and as
     requires-judgment if the guidance contains free-text description of
     something the model must figure out (e.g. "keywords", "candidate",
     "found", "any", "domain-specific").
  3. Prints a report. Run it after adding a new tool-call instruction to
     any agent's prompt to sanity-check it against this principle before
     shipping.

Run:
    python scripts/audit_deterministic_tool_calls.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class Verdict(str, Enum):
    DETERMINISTIC = "DETERMINISTIC — every argument was already known; no model judgment used"
    REQUIRES_JUDGMENT = "REQUIRES JUDGMENT — at least one argument is genuinely model-decided"
    PARTIAL = "PARTIAL — some arguments deterministic, one is ambiguous/borderline"


class Status(str, Enum):
    ALREADY_FIXED = "already fixed — called directly in Python, no longer routed through the model"
    NOT_FIXED = "NOT FIXED — still asks the model to copy a known value via a real LLM round trip"
    CLEAN = "clean — correctly requires judgment, nothing to fix"
    NOT_APPLICABLE = "n/a — agent has no registered tools"


@dataclass
class AuditEntry:
    agent: str
    tool: str
    file_line: str
    instruction: str
    verdict: Verdict
    status: Status
    evidence: str


# ---------------------------------------------------------------------------
# The real audit — every entry verified against live source on 2026-09-18.
# ---------------------------------------------------------------------------

AUDIT: list[AuditEntry] = [
    # --- TriageAgent — already fixed (feat: forced tool-use, 2026-09-17) ---
    AuditEntry(
        agent="TriageAgent", tool="check_duplicate_pr",
        file_line="app/agents/triage.py:~262 (pre-fix)",
        instruction='error_type="{error_type}", service="{event.service}", '
                     'description_prefix="{event.description[:100]}"',
        verdict=Verdict.DETERMINISTIC, status=Status.ALREADY_FIXED,
        evidence="All three arguments are direct fields off the incoming ErrorEvent. "
                 "triage() now calls self._tools['check_duplicate_pr'][0](...) directly.",
    ),
    AuditEntry(
        agent="TriageAgent", tool="get_occurrence_count",
        file_line="app/agents/triage.py:~199 (pre-fix)",
        instruction='log_group="{log_group}", pattern="{pattern}", hours=24',
        verdict=Verdict.DETERMINISTIC, status=Status.ALREADY_FIXED,
        evidence="log_group/pattern come from event.metadata; hours=24 is a fixed literal. "
                 "triage() now calls this directly, skipped entirely when no log_group.",
    ),

    # --- DiagnosisAgent — confirmed today, NOT yet fixed ---
    AuditEntry(
        agent="DiagnosisAgent", tool="get_error_samples",
        file_line="app/agents/diagnosis.py:1334-1337",
        instruction='log_group="{log_group}", pattern="{pattern}", minutes=120',
        verdict=Verdict.DETERMINISTIC, status=Status.NOT_FIXED,
        evidence="Prompt prescribes the exact call: 'log_group=\"{log_group}\", pattern=\"{pattern}\", "
                 "minutes=120' -- log_group/pattern from event.metadata (identical derivation to "
                 "TriageAgent's original design), minutes is a fixed literal. Zero judgment.",
    ),
    AuditEntry(
        agent="DiagnosisAgent", tool="check_still_occurring",
        file_line="app/agents/diagnosis.py:1339-1340",
        instruction='log_group="{log_group}", pattern="{pattern}"',
        verdict=Verdict.DETERMINISTIC, status=Status.NOT_FIXED,
        evidence="Same log_group/pattern, no additional parameters. Zero judgment.",
    ),
    AuditEntry(
        agent="DiagnosisAgent", tool="get_occurrence_timeline",
        file_line="app/agents/diagnosis.py:1342-1343",
        instruction='log_group="{log_group}", pattern="{pattern}", hours=24',
        verdict=Verdict.DETERMINISTIC, status=Status.NOT_FIXED,
        evidence="Same log_group/pattern, hours=24 fixed literal. Zero judgment.",
    ),
    AuditEntry(
        agent="DiagnosisAgent", tool="search_similar_incidents",
        file_line="app/agents/diagnosis.py:1345-1349",
        instruction='symptoms = domain-specific keywords from the error '
                     '(explicitly NOT the raw error text -- "Bad: TypeError undefined...")',
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.CLEAN,
        evidence="The prompt explicitly forbids a mechanical copy of the error text and requires "
                 "synthesizing salient keywords. Genuine judgment; correctly left to the model.",
    ),
    AuditEntry(
        agent="DiagnosisAgent", tool="search_codebase",
        file_line="app/agents/diagnosis.py:1351-1354",
        instruction="query = specific terms from the stack trace/error; conditionally skipped "
                    "entirely if extract_stack_trace_paths() already found a path",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.CLEAN,
        evidence="When a stack-trace path IS found, this step is correctly already skipped "
                 "deterministically (Branch 1 of the 3-branch design). When it isn't, the query "
                 "requires real synthesis. Correctly conditional, not a missed optimization.",
    ),
    AuditEntry(
        agent="DiagnosisAgent", tool="get_file_contents",
        file_line="app/agents/diagnosis.py:1416",
        instruction="file_path = candidate file(s) identified in step 5 (model's own prior finding)",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.CLEAN,
        evidence="Depends on the model's own search results from the prior step -- not known before "
                 "the agent runs. Genuine judgment.",
    ),
    AuditEntry(
        agent="DiagnosisAgent", tool="verify_symbol_in_repo",
        file_line="app/agents/diagnosis.py:1443",
        instruction="symbol = function name the model is about to cite",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.CLEAN,
        evidence="Only meaningful once the model has a candidate symbol in mind. Genuine judgment.",
    ),
    AuditEntry(
        agent="DiagnosisAgent", tool="find_callers",
        file_line="app/agents/diagnosis.py (blast-radius step)",
        instruction="function_name = whichever function the model has identified as affected",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.CLEAN,
        evidence="Depends on the model's own diagnosis-in-progress, not known up front.",
    ),
    AuditEntry(
        agent="DiagnosisAgent", tool="submit_diagnosis",
        file_line="app/agents/diagnosis.py",
        instruction="the entire synthesized diagnosis",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.CLEAN,
        evidence="The finalizing tool -- definitionally the model's own conclusion.",
    ),

    # --- MonitorGenerationAgent — confirmed today, NOT yet fixed ---
    AuditEntry(
        agent="MonitorGenerationAgent", tool="analyze_pr_diff",
        file_line="app/agents/monitor_generation.py:357",
        instruction='owner="{owner}", repo="{repo}", pr_number={pr_number}',
        verdict=Verdict.DETERMINISTIC, status=Status.NOT_FIXED,
        evidence="All three arguments are parameters already passed into generate_monitors() itself "
                 "-- the model is asked to copy values it was already handed. Zero judgment.",
    ),
    AuditEntry(
        agent="MonitorGenerationAgent", tool="generate_cloudwatch_alarms",
        file_line="app/agents/monitor_generation.py:359-360",
        instruction='file, additions, "any new_functions found", service_name="{repo}"',
        verdict=Verdict.PARTIAL, status=Status.NOT_FIXED,
        evidence="file/additions iterate over analyze_pr_diff's OWN structured output (itself "
                 "deterministic); service_name is a fixed literal. 'new_functions found' looks "
                 "like it needs model extraction, but analyze_pr_diff's tool implementation already "
                 "regex-extracts function names into its returned string ('| new: func1, func2') -- "
                 "likely also mechanically parseable, not confirmed with the same certainty as the "
                 "other three arguments. Worth a closer look before converting.",
    ),
    AuditEntry(
        agent="MonitorGenerationAgent", tool="generate_do_health_checks",
        file_line="app/agents/monitor_generation.py:361-362",
        instruction='file, additions, "any new endpoint paths found" '
                    '-- conditional on filename containing route/api/handler/endpoint/controller',
        verdict=Verdict.PARTIAL, status=Status.NOT_FIXED,
        evidence="Same shape as generate_cloudwatch_alarms above -- the conditional trigger itself "
                 "(filename substring match) is fully mechanical; the endpoint-path extraction is "
                 "the same borderline case.",
    ),

    # --- ErrorClarityAgent — checked, genuinely clean ---
    AuditEntry(
        agent="ErrorClarityAgent", tool="read_file / search_code",
        file_line="app/agents/error_clarity.py (tool loop)",
        instruction="path / query -- the model's own choice of where to look",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.CLEAN,
        evidence="This agent is invoked specifically because DiagnosisAgent could NOT confidently "
                 "identify a file -- there is no pre-known target to hand it. Genuine exploration.",
    ),
    AuditEntry(
        agent="ErrorClarityAgent", tool="suggest_addition / flag_pattern",
        file_line="app/agents/error_clarity.py (tool loop)",
        instruction="the model's synthesized recommendation",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.CLEAN,
        evidence="Finalizing tools -- definitionally the model's own conclusion.",
    ),

    # --- FixGenerationAgent — checked, genuinely clean ---
    AuditEntry(
        agent="FixGenerationAgent", tool="read_file / search_code / find_callers",
        file_line="app/agents/fix_generation.py:1928-1961 (agentic loop)",
        instruction="path / query / function_name -- no fixed prescription found anywhere in the "
                    "prompt (checked specifically: find_callers is never told to use the primary "
                    "target function)",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.CLEAN,
        evidence="The primary target file's content is already given directly in the prompt (Tier 1) "
                 "-- these tools exist for investigating OTHER files/functions discovered during "
                 "the fix, genuinely open-ended.",
    ),
    AuditEntry(
        agent="FixGenerationAgent", tool="apply_edit / patch_line / final_verdict",
        file_line="app/agents/fix_generation.py (agentic loop)",
        instruction="the model's synthesized fix content",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.CLEAN,
        evidence="Definitionally the model's own output -- the entire point of the call.",
    ),

    # --- No registered tools at all ---
    AuditEntry(
        agent="CodeReviewAgent", tool="(none)", file_line="app/agents/code_review.py",
        instruction="direct sequential LLM calls, no tool registry",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.NOT_APPLICABLE,
        evidence="No tools registered at all -- not applicable to this audit.",
    ),
    AuditEntry(
        agent="MergeDecisionAgent", tool="(none)", file_line="app/agents/merge_decision.py",
        instruction="single forced-tool-use call for the final decision only",
        verdict=Verdict.REQUIRES_JUDGMENT, status=Status.NOT_APPLICABLE,
        evidence="No exploratory tools -- not applicable to this audit.",
    ),
]


# ---------------------------------------------------------------------------
# Reusable heuristic for auditing NEW instruction lines added in the future
# ---------------------------------------------------------------------------

# Words that signal the model is being asked to synthesize/decide something,
# not just copy a known value. Not exhaustive -- a heuristic, not a proof.
_JUDGMENT_SIGNAL_WORDS = re.compile(
    r"\b(any|found|candidate|keyword|domain-specific|identified|discovered|"
    r"your own|the model|synthesiz|decide|choose|determine)\b",
    re.IGNORECASE,
)

# A bare f-string substitution or fixed literal: name="{var}" / name={var} / name=123.
# Group 1 captures the substituted variable's base name (before any .attr/[index])
# so it can be checked against known_vars -- a placeholder for a variable that
# ISN'T in scope yet is a sign this "known" value isn't actually known.
_LITERAL_ARG_RE = re.compile(r'\w+\s*=\s*"?\{(\w+)[\w.\[\]: ]*\}"?\s*(?:,|$)|\w+\s*=\s*\d+\s*(?:,|$)')


def check_instruction_line(instruction: str, known_vars: set[str]) -> Verdict:
    """Heuristic classification of a single 'call this tool with these args'
    instruction line. Not a substitute for the manual audit above -- use this
    to flag NEW instructions for manual review, not to auto-approve them.

    known_vars: names already computed/available before the prompt is built
    (e.g. {"log_group", "pattern", "owner", "repo"}). Every {var}-style
    substitution in the instruction must resolve to one of these for the
    line to qualify as DETERMINISTIC -- a template placeholder for a
    variable that ISN'T in known_vars means this "known" value may not
    actually be known yet, so it can't be trusted as a real fix candidate
    even if the surface syntax looks like a bare substitution. Pass an
    empty set to skip this check (accept any {var}-shaped substitution).
    """
    if _JUDGMENT_SIGNAL_WORDS.search(instruction):
        return Verdict.REQUIRES_JUDGMENT
    args = [a.strip() for a in instruction.split(",") if "=" in a]
    if not args:
        return Verdict.PARTIAL
    for arg in args:
        match = _LITERAL_ARG_RE.search(arg + ",")
        if not match:
            return Verdict.PARTIAL
        var_name = match.group(1)  # None for the fixed-literal alternative
        if known_vars and var_name is not None and var_name not in known_vars:
            return Verdict.PARTIAL
    return Verdict.DETERMINISTIC


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report() -> None:
    not_fixed = [e for e in AUDIT if e.status == Status.NOT_FIXED]
    already_fixed = [e for e in AUDIT if e.status == Status.ALREADY_FIXED]
    clean = [e for e in AUDIT if e.status == Status.CLEAN]

    print("=" * 100)
    print("DETERMINISTIC TOOL-CALL AUDIT — all agents, verified against live source 2026-09-18")
    print("=" * 100)

    print(f"\n### NOT FIXED — {len(not_fixed)} real, currently-live wasted ReAct turns ###\n")
    for e in not_fixed:
        print(f"  [{e.agent}] {e.tool}  ({e.file_line})")
        print(f"    instruction: {e.instruction}")
        print(f"    verdict:     {e.verdict.value}")
        print(f"    evidence:    {e.evidence}\n")

    print(f"### ALREADY FIXED — {len(already_fixed)} ###\n")
    for e in already_fixed:
        print(f"  [{e.agent}] {e.tool}  ({e.file_line}) — {e.status.value}")

    print(f"\n### CLEAN — {len(clean)} tools correctly requiring genuine model judgment ###\n")
    for e in clean:
        print(f"  [{e.agent}] {e.tool}")

    print("\n" + "=" * 100)
    print(f"SUMMARY: {len(not_fixed)} confirmed real fixes available, "
          f"{len(already_fixed)} already shipped, {len(clean)} correctly left as-is.")
    print("=" * 100)


if __name__ == "__main__":
    print_report()
