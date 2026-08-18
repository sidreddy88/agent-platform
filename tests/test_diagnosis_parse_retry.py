"""
Regression test for DiagnosisAgent's non-JSON-response retry.

Real production bug: a diagnosis's first attempt failed to parse (it wrote a
markdown incident report instead of JSON, after a log tool call hit a real
CloudWatch AccessDenied and derailed its output format) but its prose still
correctly identified 3 vulnerable sibling files, including crInterviewUsers.js.
diagnose()'s retry calls self.run() again -- a completely fresh, independent
ReAct loop with no memory of the failed attempt's tool calls or findings. That
second, non-deterministic run came back with valid JSON but silently dropped
crInterviewUsers.js from blast_radius entirely -- not flagged wrong, just never
mentioned again, because a second independent investigation explored less
broadly than the first.

The fix hands the retry the failed attempt's own raw text so it converts an
already-completed analysis into JSON instead of restarting the investigation
from zero. This is a pure prompt-construction test -- no LLM calls.
"""
from __future__ import annotations

from app.agents.diagnosis import DiagnosisAgent


def test_retry_prompt_includes_the_failed_answer_verbatim():
    original = "ORIGINAL PROMPT TEXT — investigate the incident..."
    failed_answer = (
        "## Blast Radius\n"
        "| routes/api/inspiringInterviewUsers.js | STILL VULNERABLE |\n"
        "| routes/api/crInterviewUsers.js | STILL VULNERABLE |\n"
    )

    retry = DiagnosisAgent._build_parse_retry_prompt(original, failed_answer)

    assert original in retry
    assert "routes/api/crInterviewUsers.js" in retry
    assert "STILL VULNERABLE" in retry


def test_retry_prompt_instructs_preserving_findings_not_reinvestigating():
    retry = DiagnosisAgent._build_parse_retry_prompt("prompt", "some analysis")

    assert "do not drop a sibling file" in retry
    assert "ONLY the JSON object" in retry


def test_retry_prompt_truncates_very_long_failed_answers():
    """Bound the retry prompt size — a runaway first attempt shouldn't blow up
    the retry's token budget."""
    huge_answer = "x" * 50_000

    retry = DiagnosisAgent._build_parse_retry_prompt("prompt", huge_answer)

    # Only the first 6000 chars of the failed answer are echoed back.
    assert huge_answer[:6000] in retry
    assert huge_answer not in retry
