"""
Trajectory grader, LLM-rubric layer: judgments the code checks can't make.

grader.py measures what's countable (redundant calls, rejections, whether a
submission was accepted, ungrounded citations). Whether the agent reasoned
well needs reading the trajectory: did it form a hypothesis, follow its
evidence, respond to rejections, and conclude from code it actually read?
Five binary questions, each answerable "yes", "no" or "na", so a human and
the model can be compared answer by answer (scripts/rubric_label_sheet.py,
scripts/rubric_agreement.py). An uncalibrated LLM grade is only an opinion;
the agreement number against hand labels is what makes it evidence.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

LLM = Callable[[str, str], Awaitable[str]]

QUESTIONS: dict[str, str] = {
    "hypothesis_early": (
        "Within its first three tool calls, did the agent commit to a specific hypothesis "
        "(a named file, function or mechanism it then investigated), rather than searching broadly?"),
    "evidence_driven": (
        "Did each tool call follow from what the previous observations showed, with no "
        "unmotivated jumps and no repeating a search that had already come back empty?"),
    "rejection_response": (
        "After each grounding-gate rejection, did the agent change what it did to address the "
        "specific problem the rejection named? Answer na if there were no rejections."),
    "conclusion_supported": (
        "Is the agent's final claim (its accepted diagnosis, or where it ended up) supported by "
        "code it actually read in this run, not reconstructed from memory or assumed?"),
    "no_wasted_turns": (
        "Did the agent avoid spending turns on steps that could not help, e.g. verifying symbols "
        "it never used, re-reading files, or calling tools that returned nothing repeatedly?"),
}
ANSWERS = ("yes", "no", "na")

SYSTEM = """You grade how an AI debugging agent worked through one task. You'll see the task
and the agent's run turn by turn: its reasoning, each tool call, and a summary of what
came back. Answer each question with exactly "yes", "no" or "na" (only where the
question says na is allowed), judging the process, not whether the final answer was
correct. Return STRICT JSON only:
{"answers": {"<question id>": "yes" | "no" | "na", ...}, "reasons": {"<question id>": "<one sentence>", ...}}"""


def render(rec: dict, max_obs: int = 400, max_thought: int = 500) -> str:
    """A trajectory record as compact text, for the judge and for a human labeller."""
    lines = [f"CASE: {rec.get('instance_id')} (trial {rec.get('trial')}), verdict {rec.get('verdict')}"]
    for s in rec.get("steps") or []:
        thought = " ".join(str(s.get("thought") or "").split())[:max_thought]
        obs = " ".join(str(s.get("output") or "").split())
        lines.append(f"\nturn {s.get('iteration')}: {s.get('name')}({str(s.get('input'))[:300]})")
        if thought:
            lines.append(f"  reasoning: {thought}")
        lines.append(f"  result ({len(obs)} chars): {obs[:max_obs]}" + (" ..." if len(obs) > max_obs else ""))
    return "\n".join(lines)


def questions_block() -> str:
    return "\n".join(f"- {qid}: {text}" for qid, text in QUESTIONS.items())


async def judge(rec: dict, llm: LLM, attempts: int = 3) -> dict:
    prompt = f"QUESTIONS:\n{questions_block()}\n\n=== RUN ===\n{render(rec)}"
    last = ""
    for _ in range(attempts):
        last = await llm(SYSTEM, prompt)
        t = last.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1].rsplit("```", 1)[0]
        try:
            data = json.loads(t)
        except json.JSONDecodeError:
            continue
        answers = data.get("answers") or {}
        if set(answers) >= set(QUESTIONS) and all(answers[q] in ANSWERS for q in QUESTIONS):
            return {"answers": {q: answers[q] for q in QUESTIONS}, "reasons": data.get("reasons") or {}}
    raise ValueError(f"judge returned no valid answers after {attempts} attempts: {last[:200]}")


def agreement(human: dict[str, dict[str, str]], model: dict[str, dict[str, str]]) -> dict:
    """Per-question and overall agreement, plus Cohen's kappa over all answers.
    Only trajectories and questions both sides answered are compared."""
    pairs: list[tuple[str, str]] = []
    per_q: dict[str, list[bool]] = {q: [] for q in QUESTIONS}
    for tid, h in human.items():
        m = model.get(tid)
        if not m:
            continue
        for q in QUESTIONS:
            if h.get(q) in ANSWERS and m.get(q) in ANSWERS:
                pairs.append((h[q], m[q]))
                per_q[q].append(h[q] == m[q])
    if not pairs:
        return {"compared": 0}
    observed = sum(a == b for a, b in pairs) / len(pairs)
    n = len(pairs)
    expected = sum((sum(a == c for a, _ in pairs) / n) * (sum(b == c for _, b in pairs) / n) for c in ANSWERS)
    kappa = (observed - expected) / (1 - expected) if expected < 1 else 1.0
    return {
        "compared": n,
        "agreement": round(observed, 3),
        "cohens_kappa": round(kappa, 3),
        "per_question": {q: (round(sum(v) / len(v), 3) if v else None) for q, v in per_q.items()},
    }
