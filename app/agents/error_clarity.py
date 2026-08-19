"""
ErrorClarityAgent — when DiagnosisAgent can't identify root cause with confidence,
this agent analyzes the error and recommends (or directly adds) targeted error
handling / logging so the NEXT occurrence is immediately diagnosable.

Two-tool design:
  suggest_addition — used when the agent finds specific code and can write the
                     exact change. Triggers a PR.
  flag_pattern     — used when the agent knows WHAT pattern is missing but cannot
                     locate the specific file/line. Records a text recommendation only.
                     No PR is created for flag_pattern calls.

Output:
  - summary: why the origin is unclear
  - additions: specific code changes (produce a PR)
  - patterns: text-only recommendations (no PR, shown in UI)
  - pr_url / pr_number: observability PR when additions were found and committed
"""
from __future__ import annotations

import json as _json
import logging
import re
from dataclasses import dataclass, field

from app.core.config import settings
from app.models.events import IncidentState
from app.services.github import GitHubError, GitHubService
from app.services.llm import LLMService

logger = logging.getLogger(__name__)

PR_BASE = "staging"

# ErrorClarityAgent's entire mandate is error VISIBILITY, not fixes — its own prompt
# says so explicitly ("Do NOT fix the bug — only add error visibility"), but nothing
# ever checked that a suggest_addition call actually honored it. Real production bug
# (VoyageGroupMag/AllInterviews#2595): given a Mongoose "reserved schema pathname"
# warning, the agent added `supressReservedKeysWarning: true` (sic — Mongoose's own
# misspelling) to a schema's options — a genuine behavior change (silences a warning)
# with zero logging or error-handling added, on a warning it was never asked to fix.
# Wrong on two independent levels: (1) it's out of scope regardless of correctness —
# ErrorClarityAgent isn't supposed to change behavior, only add visibility; (2) even
# taken as a "fix," it used the grammatically-correct spelling, not the one this
# repo's pinned mongoose@6.8.3 actually checks (`lib/schema.js` reads
# `this.options.supressReservedKeysWarning` verbatim) — a class of error no amount of
# reading the APP's own repo could catch, since the bug is in a third-party
# dependency's exact spelling, not in anything suggest_addition's existing
# code_before verbatim-match check was ever designed to verify.
# This check targets failure (1), which is both the root cause (an agent whose whole
# job is adding visibility decided a config change counted as "visibility") and the
# one actually preventable here — requiring third-party API verification for every
# addition is a much larger, separate effort (see the "how do we make agent behave
# correctly" investigation this was found under).
_OBSERVABILITY_MARKERS = re.compile(
    r"console\.(log|error|warn|debug|info)\s*\(|logger\.\w+\s*\(|\.catch\s*\(|"
    r"catch\s*\(|throw\s+|\.error\s*\(|\.warn\s*\("
)


def _adds_observability(code_before: str, code_after: str) -> bool:
    """True only if code_after introduces at least one NEW logging/error-handling
    call relative to code_before. A schema option, config flag, or any other value
    change that adds zero actual visibility fails this — regardless of whether the
    change itself would be correct, it's not what this agent exists to do."""
    before_count = len(_OBSERVABILITY_MARKERS.findall(code_before or ""))
    after_count = len(_OBSERVABILITY_MARKERS.findall(code_after or ""))
    return after_count > before_count


@dataclass
class ClarityAddition:
    file: str
    function: str
    description: str
    code_before: str    # exact verbatim snippet from the file
    code_after: str     # improved version with error handling


@dataclass
class ClarityPattern:
    description: str    # what pattern is missing and where to look
    example: str        # example of what the fix should look like


@dataclass
class ClarityResult:
    summary: str
    self_explanatory: bool = False
    additions: list[ClarityAddition] = field(default_factory=list)
    patterns: list[ClarityPattern] = field(default_factory=list)
    pr_url: str | None = None
    pr_number: int | None = None
    pr_branch: str | None = None


class ErrorClarityAgent:
    """
    Explores the codebase and identifies where better error handling / logging
    would make the error's origin unambiguous on the next occurrence.

    - When specific code is found: creates a GitHub PR (suggest_addition)
    - When pattern is known but code not found: records text recommendation (flag_pattern)

    Usage:
        agent = ErrorClarityAgent()
        result = await agent.analyze(incident)
    """

    _TOOLS = [
        {
            "name": "read_file",
            "description": "Read a file from the repository.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "name": "search_code",
            "description": "Search for a symbol or string across the repository. Returns matching file paths.",
            "input_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
        {
            "name": "suggest_addition",
            "description": (
                "Use ONLY when you have read the file and found the exact code that needs wrapping.\n"
                "code_before MUST be verbatim text copied from the file — it will be used for "
                "find-and-replace to create the PR. If you are not 100% sure of the exact text, "
                "use flag_pattern instead.\n"
                "Focus on: JSON.parse without try/catch, API calls that swallow errors, "
                "DB queries with no .catch(), callbacks that return null with no logging.\n"
                "code_after must ADD a console/logger call, a try/catch, or a .catch() — it must "
                "not just change a value, flag, or option. Silencing a warning (schema options, "
                "library config, etc.) is a fix, not observability — use flag_pattern for that "
                "instead, even if you're confident about what the fix should be: it belongs to "
                "FixGenerationAgent or a human, not you, and a wrong guess at a third-party "
                "library's exact option name is worse than no attempt."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "file": {"type": "string"},
                    "function": {"type": "string"},
                    "description": {"type": "string", "description": "What is being added and why"},
                    "code_before": {"type": "string", "description": "EXACT verbatim text from the file"},
                    "code_after": {"type": "string", "description": "Same code with error handling added"},
                },
                "required": ["file", "function", "description", "code_before", "code_after"],
            },
        },
        {
            "name": "flag_pattern",
            "description": (
                "Use when you know WHAT error handling is missing but cannot find the specific "
                "file or line — either the code search returned nothing, or the file is outside "
                "the repository (infrastructure, dependencies, upstream service).\n"
                "Records a text recommendation that is shown to the human. No PR is created."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What pattern is missing, where to look, and what to add",
                    },
                    "example": {
                        "type": "string",
                        "description": "A short code example of what the fix should look like",
                    },
                },
                "required": ["description", "example"],
            },
        },
    ]

    def __init__(self, github: GitHubService | None = None, llm: LLMService | None = None) -> None:
        self._github = github or GitHubService()
        self._llm = llm or LLMService()
        self._owner, self._repo = settings.fix_target_repo.split("/", 1)

    async def analyze(self, incident: IncidentState) -> ClarityResult:
        event = incident.error_event
        error_type = event.error_type or event.title
        branch_name = (
            f"observability/{error_type}-{incident.id[:8]}".lower()
            .replace("_", "-")
        )

        prompt = (
            f"An error occurred in production and its exact origin is unclear.\n\n"
            f"ERROR TYPE : {error_type}\n"
            f"ERROR      : {event.description}\n"
            f"SERVICE    : {event.service}\n"
            f"DIAGNOSIS  : {incident.diagnosis or '(could not identify root cause)'}\n\n"
            f"STEP 0 — Is this error already self-explanatory?\n"
            f"If the error message already names the exact file, function, and failure reason "
            f"clearly enough that a developer would know immediately what to fix — call flag_pattern "
            f"with description starting with 'SELF-EXPLANATORY:' and stop.\n\n"
            f"STEP 1 — Search for relevant code:\n"
            f"Search for the OPERATION that causes this error, not the error class name.\n"
            f"Examples:\n"
            f"  • MongoBulkWriteError → search 'bulkWrite' or 'insertMany'\n"
            f"  • S3NoSuchKey         → search 'getObject' or 's3.get'\n"
            f"  • SyntaxError JSON    → search 'JSON.parse'\n"
            f"  • TypeError undefined → search the function name from the error message\n"
            f"Use search_code with the operation keyword, then try the service name '{event.service}'.\n\n"
            f"STEP 2 — Read and inspect:\n"
            f"Use read_file on the most relevant files. Look for:\n"
            f"  • DB bulk writes / queries with no try/catch or .catch()\n"
            f"  • JSON.parse() calls with no surrounding try/catch\n"
            f"  • External API / DB calls where errors are swallowed silently\n"
            f"  • Promise chains or async functions missing .catch()\n"
            f"  • catch blocks that only rethrow without logging the original error details\n\n"
            f"STEP 3 — You MUST call either suggest_addition or flag_pattern before stopping:\n"
            f"  • Found the exact code → call suggest_addition (verbatim code_before required)\n"
            f"  • Know the pattern but can't find the file → call flag_pattern with a concrete "
            f"description of what to add and a code example\n"
            f"  • Searched and found nothing in app code → STILL call flag_pattern explaining "
            f"what error handling should be added around the relevant operation type, "
            f"with a code example of what good error handling looks like for this error class\n\n"
            f"Limit to 6 tool calls total. Do NOT fix the bug — only add error visibility."
        )

        messages: list[dict] = [{"role": "user", "content": prompt}]
        raw_additions: list[dict] = []
        raw_patterns: list[dict] = []
        _SKIP = ("node_modules", "dist/", "build/", ".min.js", ".test.", ".spec.")

        for iteration in range(14):
            try:
                text, tool_calls, stop_reason = await self._llm.complete_with_tools(
                    messages, self._TOOLS,
                    system=(
                        "You are a senior engineer improving production observability. "
                        "Your job is to identify missing error handling — not fix bugs. "
                        "When you find code: use suggest_addition with exact verbatim text. "
                        "When you can't find code: use flag_pattern to describe the recommendation."
                    ),
                )
            except Exception as exc:
                logger.error("[ErrorClarity] LLM call failed (iteration %d): %s", iteration, exc)
                break

            if stop_reason == "end_turn" or not tool_calls:
                break

            messages.append({
                "role": "assistant",
                "content": text,
                "tool_calls": [
                    {"id": tc["id"], "type": "function",
                     "function": {"name": tc["name"], "arguments": _json.dumps(tc["input"])}}
                    for tc in tool_calls
                ],
            })

            for tc in tool_calls:
                name, inp = tc["name"], tc["input"]

                if name == "read_file":
                    try:
                        content, _ = await self._github.get_file_contents(
                            self._owner, self._repo, inp.get("path", ""), ref=PR_BASE
                        )
                        result = content
                    except Exception as exc:
                        result = f"File not found: {exc}. Use flag_pattern to record a recommendation instead."

                elif name == "search_code":
                    try:
                        matches = await self._github.search_code(
                            self._owner, self._repo, inp.get("query", "")
                        )
                        paths = [r["path"] for r in matches if not any(s in r["path"] for s in _SKIP)][:10]
                        result = "\n".join(paths) if paths else (
                            "No results found. Use flag_pattern to record what should be added "
                            "without a specific file location."
                        )
                    except Exception as exc:
                        result = f"Search failed: {exc}"

                elif name == "suggest_addition":
                    if not _adds_observability(inp.get("code_before", ""), inp.get("code_after", "")):
                        result = (
                            "REJECTED: code_after doesn't add any logging or error-handling — it "
                            "changes behavior/config instead (a value, a flag, a schema option, etc.). "
                            "That's a fix, not observability, and fixing bugs is out of scope for this "
                            "agent — even a correct fix belongs to FixGenerationAgent or a human, and "
                            "an incorrect one (e.g. a third-party library option whose exact spelling "
                            "you can't verify from this repo alone) is actively worse than doing "
                            "nothing. Use flag_pattern instead to record this as a recommendation."
                        )
                    else:
                        raw_additions.append(inp)
                        result = (
                            f"✓ Specific addition recorded for {inp.get('file')}/{inp.get('function')}. "
                            f"Will be committed to a PR. Continue or stop."
                        )

                elif name == "flag_pattern":
                    raw_patterns.append(inp)
                    result = (
                        "✓ Pattern recommendation recorded — will be shown to the human. "
                        "No PR will be created for this. Continue or stop."
                    )

                else:
                    result = f"Unknown tool: {name}"

                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

        # Check for self-explanatory signal
        self_explanatory_pattern = next(
            (p for p in raw_patterns if p.get("description", "").startswith("SELF-EXPLANATORY:")), None
        )
        if self_explanatory_pattern:
            reason = self_explanatory_pattern["description"].replace("SELF-EXPLANATORY:", "").strip()
            return ClarityResult(
                summary=f"Error is already self-explanatory — no additional observability needed. {reason}",
                self_explanatory=True,
            )

        additions = [
            ClarityAddition(
                file=s["file"],
                function=s["function"],
                description=s["description"],
                code_before=s["code_before"],
                code_after=s["code_after"],
            )
            for s in raw_additions
            if s.get("code_before") and s.get("code_after")
            and s["code_before"].strip() != s["code_after"].strip()
        ]

        patterns = [
            ClarityPattern(
                description=p["description"],
                example=p.get("example", ""),
            )
            for p in raw_patterns
        ]

        if not additions and not patterns:
            return ClarityResult(
                summary=(
                    f"Could not identify specific observability gaps for '{error_type}'. "
                    f"The error likely originates outside the application code "
                    f"(infrastructure, upstream service, or dependency)."
                ),
            )

        parts: list[str] = []
        if additions:
            parts.append(f"{len(additions)} specific code location(s) found — observability PR will be created")
        if patterns:
            parts.append(f"{len(patterns)} pattern recommendation(s) where code could not be located")

        summary = f"Root cause of '{error_type}' is unclear. " + "; ".join(parts) + "."

        # Only attempt PR if there are specific additions with verbatim code
        pr_url = pr_number = None
        if additions:
            try:
                pr_url, pr_number = await self._commit_additions(incident, additions, branch_name)
            except Exception as exc:
                logger.error("[ErrorClarity] PR creation failed: %s — patterns still recorded", exc)
                if patterns:
                    summary += " (PR creation failed — see pattern recommendations below.)"

        return ClarityResult(
            summary=summary,
            additions=additions,
            patterns=patterns,
            pr_url=pr_url,
            pr_number=pr_number,
            pr_branch=branch_name if pr_url else None,
        )

    async def _commit_additions(
        self,
        incident: IncidentState,
        additions: list[ClarityAddition],
        branch_name: str,
    ) -> tuple[str, int]:
        by_file: dict[str, list[ClarityAddition]] = {}
        for a in additions:
            by_file.setdefault(a.file, []).append(a)

        base_sha = await self._github.get_branch_sha(self._owner, self._repo, PR_BASE)
        await self._github.create_branch(self._owner, self._repo, branch_name, base_sha)

        files_changed: list[str] = []
        for file_path, file_additions in by_file.items():
            try:
                content, file_sha = await self._github.get_file_contents(
                    self._owner, self._repo, file_path, ref=PR_BASE
                )
            except GitHubError as exc:
                logger.warning("[ErrorClarity] Could not fetch %s: %s — skipping", file_path, exc)
                continue

            new_content = content
            applied = 0
            for a in file_additions:
                if a.code_before in new_content:
                    new_content = new_content.replace(a.code_before, a.code_after, 1)
                    applied += 1
                elif a.code_before.strip() in new_content:
                    new_content = new_content.replace(a.code_before.strip(), a.code_after.strip(), 1)
                    applied += 1
                else:
                    logger.warning(
                        "[ErrorClarity] code_before not found verbatim in %s (%s) — skipping",
                        file_path, a.function,
                    )

            if applied == 0 or new_content == content:
                continue

            await self._github.update_file(
                self._owner, self._repo, file_path, new_content,
                f"observability: add error handling in {file_path.split('/')[-1]}",
                branch_name, file_sha,
            )
            files_changed.append(file_path)

        if not files_changed:
            raise RuntimeError(
                "No additions matched verbatim — code may have changed or code_before was approximate. "
                "Pattern recommendations are still recorded."
            )

        event = incident.error_event
        additions_summary = "\n".join(
            f"- **{a.file}** `{a.function}`: {a.description}" for a in additions
        )
        pr_body = (
            f"## Observability Improvement\n\n"
            f"This PR **does not fix the underlying bug**. It adds error handling and logging "
            f"so the next occurrence of this error will be immediately diagnosable.\n\n"
            f"**Error:** `{event.error_type or event.title}` in `{event.service}`  \n"
            f"**Incident:** {incident.id}  \n\n"
            f"## Changes\n{additions_summary}\n"
        )

        pr_number, pr_url = await self._github.create_pull_request(
            self._owner, self._repo,
            title=f"observability({event.service}): add error handling for {event.error_type or event.title}",
            body=pr_body,
            head=branch_name,
            base=PR_BASE,
            labels=["observability", "error-handling"],
        )
        logger.info("[ErrorClarity] Observability PR #%d: %s", pr_number, pr_url)
        return pr_url, pr_number
