"""The triage gate marks fallback answers (step limit, unparseable output) as no_answer instead of scoring them."""
from types import SimpleNamespace

from app.agents.triage import _parse_triage_result
from scripts.eval_triage_regression import _no_answer


def test_step_limit_fallback_is_no_answer():
    r = _parse_triage_result("I was unable to find an answer within the allowed number of steps.")
    assert (r.decision, r.severity) == ("real", "P2")           # the fallback that used to be scored as an answer
    assert _no_answer(r) == "step_limit"


def test_unparseable_answer_is_no_answer():
    assert _no_answer(_parse_triage_result("decision: real, severity: P2")) == "parse_error"


def test_a_real_answer_is_not():
    r = _parse_triage_result('{"decision": "noise", "severity": "P3", "reasoning": "x"}')
    assert _no_answer(r) is None
    assert _no_answer(SimpleNamespace(reasoning=None)) is None
