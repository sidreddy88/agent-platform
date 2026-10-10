"""
Agent profiles: everything the harness optimizer needs that differs between the
agent being optimized. The loop, acceptance rules, budget, state, history and
report are shared; the profile supplies the harness root, setting bounds, the
proposer's components, the trajectory grader, health baselines and the proposer
and critic instructions.

One optimizer run optimizes one agent, so the active profile is process-wide:
scripts/optimize_harness.py --agent fix calls use("fix") before building
anything. The default is "diagnosis", so existing runs and tests are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

from app.agents.harness import DEFAULT_ROOT


@dataclass(frozen=True)
class AgentProfile:
    name: str
    harness_root: Path
    setting_bounds: dict
    components: tuple
    component_files: dict[str, Callable[[str], bool]]
    proposer_system: str
    critic_system: str
    grader: ModuleType
    health_baseline: dict
    component_families: dict      # component -> family, for the loop's exploration rule


FIX_COMPONENTS = ("prompts", "skills", "tool_descriptions", "settings")

FIX_PROPOSER_SYSTEM = """You improve the harness of FixGenerationAgent: the prompt text, tool descriptions,
settings and the skills file around a frozen model. Given a diagnosed bug (file and function), the
agent reads the code with tools (read_file, search_code, find_callers) and makes the fix with
apply_edit / patch_line, or declines with no_edit. Its fix is graded by the repository's own tests.

You will see the current harness files, evidence from the agent's recent runs (what it did, where
it failed or wasted turns, what it cost; the tests' contents are never shown), and the history of
edits already tried and how they scored.

Propose TWO alternative edits, each exactly one mechanism, one component and one hypothesis, with
different mechanisms (ideally different components), and say which you expect to help more. Target
failures or waste you can point to in the evidence. The ACCEPTANCE RULES section says what this run
can detect: an edit is accepted if it raises the resolve rate by more than the noise band (it may
then cost more), or if it is cheaper by more than cost noise at an unchanged resolve rate.

The skills component is skills.prompt: short, general lessons the agent reads before fixing (it is
shown to the agent once it has at least one "- " bullet). Grow it one bullet at a time: add a bullet
(find the header line or the last bullet, replace it with itself plus the new "- " bullet), or edit
one existing bullet. Never rewrite the whole file and never put task-specific content in it.

Never instruct the agent to be brief, to save tokens or to do less: that lowers accuracy. Change what
the harness asks for, not how hard the model tries. Do not add emphasis (MUST, NEVER, CRITICAL).
Removing text that isn't earning its place is a valid edit.

Constraints: the edit must help on JavaScript production incidents as well as the Python
repositories you see here. Never mention specific tasks, repositories, file paths, function names or
answers from the evidence. Never weaken the root-cause rules (fix the cause, not the symptom; no null
guards or try/except at crash sites), never tell the agent to edit tests, and never make declining
(no_edit) more attractive to look cheaper. Do not repeat an edit the history shows was rejected.
Evaluation-only condition you must not optimise for: find_callers often returns nothing here (the
call graph doesn't index these repositories); in production it works, so don't discourage it.

Settings are behaviour, not just numbers: read the SETTINGS section for what each one does and its
allowed range. Do not re-propose an idea from REJECTED IDEAS unless the evidence shows something new.

Return STRICT JSON only:
{"candidates": [
   {"component": one of %s,
    "hypothesis": "what failure/waste this fixes and why the edit should fix it",
    "edits": [{"file": "<harness file name>", "find": "<exact text currently in the file>",
               "replace": "<new text>"}]},
   {... a second, different candidate ...}],
 "preferred": 0 or 1,
 "why": "why the preferred one should help more"}
Each "find" must occur exactly once in its file.""" % (list(FIX_COMPONENTS),)

FIX_CRITIC_SYSTEM = """You are a strict reviewer of changes to an agent harness in an automated
evolution loop. The harness is evolved against the very tasks it is measured on, so anti-overfitting
review is critical. The change was written by another model in response to aggregated failure modes.
Catch changes that would be cheating, degenerate, or self-destructive.

The agent: FixGenerationAgent, which fixes a diagnosed bug (reads code with read_file, search_code and
find_callers; edits with apply_edit / patch_line; can decline with no_edit). The harness you are
reviewing is its prompt text, tool descriptions, a few numeric settings and skills.prompt (general
lessons). Evolution runs on SWE-bench Python repositories, graded by their tests. Its production job
is fixing incidents in a JavaScript/Node application. A good change helps on both.

EVALUATION-ONLY CONDITION: find_callers often returns nothing here (the call graph doesn't index these
repositories); in production it works. A change that discourages or removes it is overfitting:
reject it under 1.

REJECT if ANY of:

1. LEAKAGE / TASK-SPECIALIZATION: the diff encodes knowledge of specific evaluation tasks: ids,
   repository or project names, file paths, function or class names from particular repos, expected
   fixes, or branching on task-identifying features. General engineering practice is fine. Litmus
   test: would it still make sense, and help, on a JavaScript production incident?
2. DEGENERATE: a no-op claiming a mechanism, or it weakens a safety mechanism without a working
   replacement: the root-cause rules (no symptom fixes, no null guards or try/except at crash
   sites), verifying old text before editing, or declining honestly when the bug isn't in the file.
   Making the agent decline (no_edit) more often, or give up sooner, to look cheaper is degenerate.
3. GRADER GAMING: the change targets how fixes are scored rather than fixing better: editing or
   skipping tests, broad exception swallowing to make tests pass, special-casing test inputs.
4. UNDECLARED BUNDLING: changes not covered by the declared component and hypothesis, or one edit
   secretly bundling several independent mechanisms.
5. MEMORY LEAKAGE: task-specific data (file contents, answers, repo layouts, names) injected into
   the prompt or skills.prompt as if it were general guidance.
6. UNBOUNDED WORK: an added check or "keep verifying" instruction with no exit, or anything that
   could spend the whole turn budget without finishing. (A numeric setting changed within its
   allowed range is bounded by code and is NOT unbounded work.)
7. NEAR-DUPLICATE: re-proposes an idea listed under ALREADY REJECTED without new evidence.
8. FALSE TOOL CLAIM: added text states how a tool behaves in a way that contradicts the tool's own
   description (shown under CURRENT TOOL DESCRIPTIONS).
9. SKILLS REWRITE: replaces most of skills.prompt instead of adding or editing one bullet.

Otherwise ACCEPT. Review intent and content, not style or wording quality.
Return STRICT JSON only:
{"verdict": "accept" | "reject", "reasons": ["..."], "risk_notes": ["..."]}"""

FIX_COMPONENT_FILES = {
    "prompts": lambda f: f.endswith(".prompt") and f != "skills.prompt",
    "skills": lambda f: f == "skills.prompt",
    "tool_descriptions": lambda f: f == "tool_descriptions.json",
    "settings": lambda f: f == "settings.json",
}

FIX_SETTING_BOUNDS = {
    "max_fix_turns": (8, 40),
    "budget_warning_turns": (1, 8),
    "no_edit_nudges": (0, 3),
    "max_cutoffs": (1, 6),
    # Test mode (app/services/repo_tests.py): run the repo's existing tests before
    # and after a fix, regenerate on regressions. A frozenset lists allowed values.
    "test_mode": frozenset({"off", "existing_tests"}),
    "test_max_attempts": (1, 4),
    "test_max_files": (1, 6),
    "test_timeout_s": (60, 600),
    "test_pick": frozenset({"fewest_broken", "last"}),
}

# From the run-2 fix step (DeepSeek-V4.1-Flash, library policy): ~$0.046 per fix,
# 300/429 localized cases resolved. find_callers is often empty on these repos.
FIX_HEALTH_BASELINE = {
    "tool_failure_rate": {"read_file": 0.05, "search_code": 0.30, "find_callers": 0.60},
    "cost_per_trial_usd": 0.05,
    # The fix evolve set is chosen from cases the current harness fails (re-measured at
    # 5-9%), so round 0's pass rate is low by design; --agent fix also turns the pass-rate
    # tripwire off (fix-r1 tripped on 2/20 against the old 0.70).
    "pass_rate": 0.08,
}


def _diagnosis() -> AgentProfile:
    from app.harness_optimizer import candidates, critic, grader, health, proposer
    return AgentProfile(
        name="diagnosis", harness_root=DEFAULT_ROOT / "diagnosis",
        setting_bounds=candidates.SETTING_BOUNDS, components=proposer.COMPONENTS,
        component_files=proposer.COMPONENT_FILES, proposer_system=proposer.SYSTEM,
        critic_system=critic.SYSTEM, grader=grader, health_baseline=health.BASELINE,
        component_families={"task_prompt": "prompt", "prompt_fragments": "prompt",
                            "tool_descriptions": "tools", "settings": "settings"},
    )


def _fix() -> AgentProfile:
    from app.harness_optimizer import fix_grader
    return AgentProfile(
        name="fix", harness_root=DEFAULT_ROOT / "fix", setting_bounds=FIX_SETTING_BOUNDS,
        components=FIX_COMPONENTS, component_files=FIX_COMPONENT_FILES,
        proposer_system=FIX_PROPOSER_SYSTEM, critic_system=FIX_CRITIC_SYSTEM,
        grader=fix_grader, health_baseline=FIX_HEALTH_BASELINE,
        component_families={"prompts": "prompt", "skills": "skills",
                            "tool_descriptions": "tools", "settings": "settings"},
    )


_BUILDERS = {"diagnosis": _diagnosis, "fix": _fix}
_active = "diagnosis"


def use(name: str) -> AgentProfile:
    global _active
    if name not in _BUILDERS:
        raise ValueError(f"unknown agent profile {name!r}; known: {sorted(_BUILDERS)}")
    _active = name
    return active()


def active() -> AgentProfile:
    return _BUILDERS[_active]()
