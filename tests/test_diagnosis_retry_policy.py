"""DiagnosisAgent's retry-on-no-submission policy (harness setting retry_on_no_submission)."""
import asyncio
import json
import shutil

from app.agents.diagnosis import DiagnosisAgent, DiagnosisResult
from app.agents.harness import DEFAULT_ROOT
from app.harness_optimizer import candidates
from app.models.events import ErrorEvent, EventSource, IncidentState


def _agent(tmp_path, retries):
    d = tmp_path / f"h{retries}"
    shutil.copytree(DEFAULT_ROOT / "diagnosis", d)
    s = json.loads((d / "settings.json").read_text())
    s["retry_on_no_submission"] = retries
    (d / "settings.json").write_text(json.dumps(s))
    from app.agents.harness import load_harness
    agent = DiagnosisAgent.__new__(DiagnosisAgent)       # no services needed for the policy
    agent._harness = load_harness("diagnosis", d)
    return agent


def _incident():
    return IncidentState(error_event=ErrorEvent(source=EventSource.APPLICATION, error_type="X",
                                                title="t", description="d", service="s", metadata={}))


def _run(agent, outcomes):
    calls = []

    async def once(incident, prior_context=None):
        calls.append(1)
        ok = outcomes[len(calls) - 1]
        return (DiagnosisResult(root_cause="r", confidence=0.9, affected_file="a.py") if ok else
                DiagnosisResult(root_cause="none", confidence=0.0, escalate=True, no_submission=True))
    agent._diagnose_once = once
    return asyncio.run(agent.diagnose(_incident())), calls


def test_retries_once_after_no_submission_and_keeps_the_second_answer(tmp_path):
    result, calls = _run(_agent(tmp_path, 1), [False, True])
    assert len(calls) == 2 and result.affected_file == "a.py" and not result.no_submission
    assert [a["no_submission"] for a in result.attempts] == [True, False]


def test_off_by_default_and_never_retries_a_real_answer(tmp_path):
    result, calls = _run(_agent(tmp_path, 0), [False])
    assert len(calls) == 1 and result.no_submission
    result, calls = _run(_agent(tmp_path, 2), [True])
    assert len(calls) == 1 and len(result.attempts) == 1


def test_gives_up_after_the_configured_retries(tmp_path):
    result, calls = _run(_agent(tmp_path, 2), [False, False, False])
    assert len(calls) == 3 and result.no_submission and len(result.attempts) == 3


def test_setting_is_bounded_for_the_optimizer(tmp_path):
    cand = tmp_path / "c"
    shutil.copytree(DEFAULT_ROOT / "diagnosis", cand)
    s = json.loads((cand / "settings.json").read_text())
    s["retry_on_no_submission"] = 5
    (cand / "settings.json").write_text(json.dumps(s))
    import pytest
    with pytest.raises(candidates.InvalidCandidate):
        candidates.validate(DEFAULT_ROOT / "diagnosis", cand)
