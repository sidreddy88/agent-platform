"""
RAG judge — samples real DiagnosisAgent runs and scores whether the generated
diagnosis actually stayed faithful to the code it retrieved, and whether it
addressed the incident at all.

This is a probabilistic, sampled observability signal, not a gate.
DiagnosisAgent already has a deterministic grounding gate (submit_diagnosis
rejects claims that don't verify against the real repo) — this doesn't
replace that. It answers a different question: even when a diagnosis passes
grounding, does it represent what the retrieved code actually says?

Sampled at ~10% of real diagnoses (JUDGE_SAMPLE_RATE), not every run —
judging every diagnosis would double LLM cost for every incident, and this
project's own established finding (the TriageAgent fine-tune regression-gate
work) is that an LLM-judged signal needs to be read as a trend across many
samples, not trusted as a single-case verdict — a 10% sample is enough to
produce that trend at a tenth of the cost of judging everything.

Usage:
    from app.services.rag_judge import should_sample, judge_diagnosis_faithfulness

    if should_sample():
        result = await judge_diagnosis_faithfulness(question, chunks, diagnosis_text)
"""
from __future__ import annotations

import json
import logging
import random
import re

from app.services.llm_gateway import llm_gateway

logger = logging.getLogger(__name__)

JUDGE_SAMPLE_RATE = 0.10

JUDGE_PROMPT = """\
You are evaluating a RAG-grounded incident diagnosis for faithfulness and \
relevance. Score two dimensions from 0.0 to 1.0.

Incident: {question}

Retrieved code context:
{context}

Generated diagnosis (root cause): {answer}

faithfulness: does the diagnosis's claims stay within what the retrieved \
context actually shows? 1.0 = every claim is supported by the context. \
0.0 = the diagnosis contradicts or ignores the context.
relevance: does the diagnosis actually address the incident? 1.0 = fully \
addresses it. 0.0 = does not address it at all.

Respond with ONLY a JSON object, no preamble: \
{{"faithfulness": <float>, "relevance": <float>, "notes": "<one-line explanation>"}}\
"""


def should_sample(rate: float = JUDGE_SAMPLE_RATE) -> bool:
    return random.random() < rate


_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _strip_code_fence(text: str) -> str:
    """Haiku reliably wraps JSON output in a ```json ... ``` fence despite
    being asked for "ONLY a JSON object, no preamble" — this is the same
    class of real-world LLM-output gap this whole series has run into
    elsewhere: what's asked for and what's produced aren't automatically
    the same thing. Strip the fence before parsing rather than trust it away."""
    return _CODE_FENCE_RE.sub("", text.strip()).strip()


async def judge_diagnosis_faithfulness(
    question: str, context_chunks: list[str], answer: str,
) -> dict | None:
    """Score a diagnosis against the code it actually retrieved.

    Returns None on any failure or missing input — this is an observability
    signal only, never allowed to affect the real pipeline or raise into it.
    """
    if not context_chunks or not answer:
        return None
    context = "\n\n".join(context_chunks)[:6000]
    try:
        llm_service = llm_gateway.get_llm_service_for("rag_judge")
        raw = await llm_service.complete(messages=[{
            "role": "user",
            "content": JUDGE_PROMPT.format(
                question=question[:1000], context=context, answer=answer[:1500],
            ),
        }])
        scores = json.loads(_strip_code_fence(raw))
        return {
            "faithfulness": float(scores["faithfulness"]),
            "relevance": float(scores["relevance"]),
            "notes": scores.get("notes", ""),
        }
    except Exception as exc:
        logger.debug("[RAGJudge] Faithfulness scoring failed: %s", exc)
        return None
