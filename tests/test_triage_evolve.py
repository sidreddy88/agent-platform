"""evolve/triage: the harness-evolve case runner and config for TriageAgent. No model calls: the agent's LLM is
replaced by a scripted ReAct conversation."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evolve" / "triage"))

import run_case  # noqa: E402

from app.agents.harness import DEFAULT_ROOT  # noqa: E402

CASE = "synth_070bfd0a"          # in the evolve sample: real/P1, has a log group


def _answer(decision: str, severity: str) -> str:
    return json.dumps({"decision": decision, "severity": severity, "blast_radius": "single_service",
                       "occurrences_24h": 150, "duplicate_pr": None, "reasoning": "frequent and urgent"})


def _script(monkeypatch, replies: list[str], seen: list | None = None) -> None:
    """The model's replies, in order; records each call's system prompt and first user message."""
    from app.services.llm import LLMService

    it = iter(replies)

    async def complete(self, messages, system=None, **kwargs):
        if seen is not None:
            seen.append({"system": system, "task": messages[0]["content"], "model": self._model})
        self.last_input_tokens, self.last_output_tokens = 1000, 100
        return next(it)

    monkeypatch.setattr(LLMService, "complete", complete)
    monkeypatch.delenv("TRIAGE_MODEL", raising=False)


def _full_run(decision="real", severity="P1", call_occurrences=True) -> list[str]:
    replies = ['Thought: check PRs\nAction: check_duplicate_pr\nAction Input: {"error_type": "X", "service": "s"}']
    if call_occurrences:
        replies.append('Thought: count\nAction: get_occurrence_count\n'
                       'Action Input: {"log_group": "g", "pattern": "X", "hours": 24}')
    return replies + [f"Thought: done\nAnswer: {_answer(decision, severity)}"]


def run(harness=DEFAULT_ROOT / "triage", case=CASE) -> dict:
    import asyncio

    return asyncio.run(run_case.run(str(harness), case))


def test_a_correct_answer_passes_with_cost_steps_and_a_hidden_label(monkeypatch):
    _script(monkeypatch, _full_run())
    r = run()
    assert r["passed"] and r["submitted"]
    assert r["cost_usd"] > 0
    assert [s["name"] for s in r["steps"]] == ["check_duplicate_pr", "get_occurrence_count"]
    assert r["answer"] == "expected real/P1"
    assert "did not call" not in r["detail"]


def test_a_skipped_occurrence_lookup_is_reported_to_the_proposer(monkeypatch):
    _script(monkeypatch, _full_run(call_occurrences=False))
    r = run()
    assert r["passed"]                                  # the label matched, but the evidence says how
    assert "did not call get_occurrence_count" in r["detail"]


def test_a_wrong_severity_fails(monkeypatch):
    _script(monkeypatch, _full_run(severity="P3"))
    assert not run()["passed"]


def test_no_usable_answer_fails_even_when_the_fallback_matches(monkeypatch):
    _script(monkeypatch, ["Thought: I think this is real, P2.\nAnswer: real, P2"])
    r = run(case=next(c for c, v in run_case.cases().items()
                      if v["output"]["decision"] == "real" and v["output"]["severity"] == "P2"))
    assert not r["passed"] and not r["submitted"] and "no usable answer" in r["detail"]


def test_the_candidate_harness_is_what_the_agent_runs(monkeypatch, tmp_path):
    cand = tmp_path / "cand"
    shutil.copytree(DEFAULT_ROOT / "triage", cand)
    (cand / "task_prompt.prompt").write_text("CANDIDATE PROMPT for {title}")
    seen: list = []
    _script(monkeypatch, _full_run(), seen)
    run(cand)
    assert seen[0]["task"].startswith("CANDIDATE PROMPT for ")


def test_triage_model_override(monkeypatch):
    seen: list = []
    _script(monkeypatch, _full_run(), seen)
    monkeypatch.setenv("TRIAGE_MODEL", "claude-haiku-4-5")
    r = run()
    assert seen[0]["model"] == "claude-haiku-4-5"
    assert r["cost_usd"] > 0                             # priced from its dated id in llm_routing.json


def test_billing_errors_stop_the_run_instead_of_failing_the_case(monkeypatch):
    import anthropic
    import httpx

    from app.services.llm import LLMService

    async def complete(self, *a, **k):
        raise anthropic.RateLimitError("rate limited", response=httpx.Response(429, request=httpx.Request("POST", "https://x")),
                                       body=None)

    monkeypatch.setattr(LLMService, "complete", complete)
    r = run()
    assert r["provider_failure"].startswith("RateLimitError")


def test_grader_matches_every_anchor(tmp_path):
    for line in (ROOT / "evolve" / "triage" / "anchors.jsonl").read_text().splitlines():
        a = json.loads(line)
        out = tmp_path / "out.txt"
        out.write_text(a["output"])
        assert run_case.grade_only(a["case"], str(out))["passed"] is a["passed"], a["why"]


def test_evolve_and_held_out_cases_do_not_overlap():
    pytest.importorskip("harness_evolve")
    sys.path.insert(0, str(ROOT / "evolve" / "triage"))
    import config

    evolve = config.evolve_sample(60)
    held = {c["id"] for c in config._ids(config.HELDOUT)}
    assert len(evolve) == len(set(evolve)) == 60
    assert not set(evolve) & held
    assert evolve == config.evolve_sample(60)            # seeded: the same cases every run
