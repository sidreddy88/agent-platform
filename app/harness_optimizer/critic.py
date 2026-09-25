"""
Leakage critic: screens a candidate's diff BEFORE any money is spent on it.

Adapted from RRSI's critic (rrsi/critic.py, github.com/google-research/rrsi,
Apache-2.0): a deterministic precheck, then an LLM review of intent and
content. The six reject classes and their wording follow RRSI's prompt,
rewritten for our domain. A rejection goes back to the proposer for a
bounded number of repairs; a candidate that can't be repaired is recorded in
the history without a measurement.

Our domain twist on RRSI's litmus test ("would this still help on an
unfamiliar task from a different suite?"): the harness is evolved on
SWE-bench Python repos, but its production job is diagnosing incidents in a
JavaScript app from CloudWatch logs. A change that only makes sense for
Python/SWE-bench is overfitting even if it's generic within SWE-bench.
"""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

LLM = Callable[[str, str], Awaitable[str]]   # (system, prompt) -> text

GENERIC_PATTERNS = [
    (r"sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}", "credential in diff"),
]

SYSTEM = """You are a strict reviewer of changes to an agent harness in an automated
evolution loop. The harness is evolved against the very tasks it is measured on,
so anti-overfitting review is critical. The change was written by another model
in response to aggregated failure modes. Catch changes that would be cheating,
degenerate, or self-destructive.

The agent: DiagnosisAgent, which diagnoses the root cause of a software incident
(finds the file/function to fix, with verbatim evidence from the code) using
tools that read, grep and search a repository. The harness you are reviewing is
its prompt text, tool descriptions and a few numeric settings.

Evolution runs on SWE-bench Python repositories. Its production job is different:
incidents in a JavaScript/Node application, reported from CloudWatch logs. A good
change helps on both. A change that only makes sense for Python, for SWE-bench, or
for particular repositories is overfitting even if it looks general.

REJECT if ANY of:

1. LEAKAGE / TASK-SPECIALIZATION: the diff encodes knowledge of specific evaluation
   tasks: task or issue ids, repository or project names, file paths, function or
   class names from particular repos, expected answers, magic constants that only
   make sense for one task, or branching on task-identifying features ("if the issue
   mentions X"). General debugging practice is fine. Litmus test: would this change
   still make sense, and still help, on a JavaScript production incident from
   CloudWatch logs?
2. DEGENERATE: the diff is effectively a no-op while claiming a mechanism, or it
   deletes or weakens a safety mechanism without a working replacement: the
   requirement to ground claims in verbatim code, to verify symbols before naming
   them, to call submit_diagnosis, or to escalate/lower confidence when evidence is
   missing. Making the agent give up sooner to look cheaper is degenerate.
3. GRADER GAMING: the change tries to infer, target or influence how diagnoses are
   scored (e.g. naming more candidate files to raise the chance of a match, or
   steering toward files a grader might accept) rather than improving diagnosis.
4. UNDECLARED BUNDLING: the diff contains changes not covered by the declared
   component and hypothesis, or one declared edit secretly bundles several
   independent mechanisms.
5. MEMORY LEAKAGE: the change injects task-specific data (file contents, answers,
   repo layouts, names) into the prompt as if it were general guidance.
6. UNBOUNDED WORK: an added check, retry or "keep verifying" instruction with no
   exit, or anything that could spend the whole turn budget without finishing.

Otherwise ACCEPT. Review intent and content, not style or wording quality.
Return STRICT JSON only:
{"verdict": "accept" | "reject", "reasons": ["..."], "risk_notes": ["..."]}"""


@dataclass
class Review:
    accept: bool
    reasons: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)


def domain_patterns(case_ids: list[str], repos: list[str], truth_paths: list[str]) -> list[tuple[str, str]]:
    """Denylist from the evaluation data itself: any evolve OR held-out case id,
    repo name, or file path touched by a true fix is leakage by definition."""
    pats = []
    for cid in case_ids:
        pats.append((re.escape(cid), f"names evaluation case {cid}"))
    for repo in repos:
        owner, _, name = repo.partition("/")
        for token in {owner, name} - {""}:
            if len(token) >= 4:
                pats.append((rf"(?i)\b{re.escape(token)}\b", f"names repository {repo}"))
    for path in truth_paths:
        pats.append((re.escape(path), f"names a file touched by a true fix ({path})"))
    return pats


def precheck(diff: str, patterns: list[tuple[str, str]]) -> list[str]:
    # Only added lines count: removing a leaky line is fine.
    added = "\n".join(line[1:] for line in diff.splitlines()
                      if line.startswith("+") and not line.startswith("+++"))
    return sorted({why for pat, why in GENERIC_PATTERNS + patterns if re.search(pat, added)})


async def review(diff: str, component: str, hypothesis: str, llm: LLM,
                 patterns: list[tuple[str, str]], attempts: int = 3) -> Review:
    hard = precheck(diff, patterns)
    if hard:
        return Review(False, [f"precheck: {h}" for h in hard])
    if not diff.strip():
        return Review(False, ["empty diff"])
    payload = (f"DECLARED COMPONENT: {component}\nHYPOTHESIS: {hypothesis}\n\n"
               f"=== DIFF ===\n{diff[:60_000]}")
    last = ""
    for _ in range(attempts):
        last = await llm(SYSTEM, payload)
        try:
            v = json.loads(_strip_fences(last))
        except json.JSONDecodeError:
            continue
        if isinstance(v, dict) and v.get("verdict") in ("accept", "reject"):
            return Review(v["verdict"] == "accept", list(v.get("reasons") or []),
                          list(v.get("risk_notes") or []))
    return Review(False, [f"critic returned no valid verdict after {attempts} attempts: {last[:200]}"])


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()
