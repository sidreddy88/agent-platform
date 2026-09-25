"""
Proposer: reads the evidence and proposes ONE edit to the harness.

GEPA-style reflective mutation (Agrawal et al., "GEPA: Reflective Prompt
Evolution Can Outperform Reinforcement Learning", ICLR 2026): the proposer
sees the agent's actual execution traces and the edit history, diagnoses in
natural language what went wrong, and targets the edit at that failure,
instead of a random perturbation of a scalar score.

One declared edit per candidate is RRSI's annealed L0 budget at its
floor (b_min = 1). With a handful of rounds there is nothing to anneal, and a
single edit keeps every measured change attributable to one mechanism.

The edit is returned as exact search/replace operations on harness files,
not full file rewrites: task_prompt.prompt is ~17k chars, and asking a model
to reproduce it verbatim to change one paragraph is expensive and invites
silent drift in the parts it didn't mean to touch.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.harness_optimizer.candidates import InvalidCandidate, harness_files

LLM = Callable[[str, str], Awaitable[str]]

COMPONENTS = ("task_prompt", "prompt_fragments", "tool_descriptions", "settings")

SYSTEM = """You improve the harness of DiagnosisAgent: the prompt text, tool descriptions
and settings around a frozen model. The agent diagnoses the root cause of software
incidents with repository tools (read files, grep, search, verify symbols) and must
finalize by calling submit_diagnosis with verbatim evidence from the code.

You will see the current harness files, evidence from the agent's recent runs (what
it did, where it failed or wasted turns, what it cost), and the history of edits
already tried and how they scored.

Propose exactly ONE edit: one mechanism, one component, one hypothesis. Target a
failure or waste you can point to in the evidence. Prefer changes that make the
agent reach an accepted diagnosis in fewer turns or with less resent context, since
the objective is lower cost per case at no loss of accuracy; score gains are hard
to measure at this sample size. Describe how tools behave rather than adding
commands; do not add emphasis (MUST, NEVER, CRITICAL). Removing text that isn't
earning its place is a valid edit.

Constraints: the edit must help on JavaScript production incidents from CloudWatch
logs as well as the Python repositories you see here. Never mention specific tasks,
repositories, file paths or answers from the evidence. Never weaken the grounding
requirements (verbatim snippets, symbol verification, calling submit_diagnosis).
Do not repeat an edit the history shows was already rejected.
Evaluation-only conditions you must not optimise for: search_similar_incidents always
returns nothing here (no past-incident knowledge base for these repositories) and the
CloudWatch log tools have no data. Both carry real information in production, so don't
discourage or remove them.

Return STRICT JSON only:
{"component": one of %s,
 "hypothesis": "what failure/waste this fixes and why the edit should fix it",
 "edits": [{"file": "<harness file name>", "find": "<exact text currently in the file>",
            "replace": "<new text>"}]}
Each "find" must occur exactly once in its file.""" % (list(COMPONENTS),)


@dataclass
class Proposal:
    component: str
    hypothesis: str
    edits: list[dict] = field(default_factory=list)


def build_prompt(harness_dir: Path, evidence: str, history: str, feedback: str = "") -> str:
    files = harness_files(harness_dir)
    parts = ["=== CURRENT HARNESS FILES ==="]
    for name, text in files.items():
        parts.append(f"--- {name} ({len(text)} chars) ---\n{text}")
    parts.append(f"=== EVIDENCE FROM RECENT RUNS ===\n{evidence}")
    parts.append(f"=== EDIT HISTORY ===\n{history}")
    if feedback:
        parts.append(f"=== YOUR PREVIOUS ATTEMPT THIS ROUND WAS REJECTED ===\n{feedback}\n"
                     f"Fix those problems, or propose a different edit.")
    return "\n\n".join(parts)


def parse(text: str) -> Proposal:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1].rsplit("```", 1)[0]
    try:
        data = json.loads(t)
    except json.JSONDecodeError as exc:
        raise InvalidCandidate(f"proposer did not return valid JSON: {exc}") from exc
    comp = data.get("component")
    if comp not in COMPONENTS:
        raise InvalidCandidate(f"component must be one of {COMPONENTS}, got {comp!r}")
    edits = data.get("edits") or []
    if not edits or not all(isinstance(e, dict) and {"file", "find", "replace"} <= set(e) for e in edits):
        raise InvalidCandidate("edits must be a non-empty list of {file, find, replace}")
    return Proposal(comp, str(data.get("hypothesis", "")).strip(), edits)


def apply_ops(harness_dir: Path, edits: list[dict]) -> dict[str, str]:
    """Search/replace ops -> {file: new full content}. Each find must match once."""
    files = harness_files(harness_dir)
    out: dict[str, str] = {}
    for e in edits:
        name = e["file"]
        if name not in files:
            raise InvalidCandidate(f"no harness file named {name!r}")
        text = out.get(name, files[name])
        n = text.count(e["find"])
        if n != 1:
            raise InvalidCandidate(f"{name}: find text occurs {n} times (must be exactly 1): "
                                   f"{e['find'][:80]!r}")
        out[name] = text.replace(e["find"], e["replace"])
    return out


async def propose(harness_dir: Path, evidence: str, history: str, llm: LLM,
                  feedback: str = "") -> tuple[Proposal, dict[str, str]]:
    text = await llm(SYSTEM, build_prompt(harness_dir, evidence, history, feedback))
    proposal = parse(text)
    return proposal, apply_ops(harness_dir, proposal.edits)
