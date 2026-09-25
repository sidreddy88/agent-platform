"""Cost-by-source attribution and the trajectory capture that feeds it."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import cost_meter
from scripts.analyze_cost_by_source import analyze, attribute_call
from scripts.eval_swebench_diagnosis import _capture_llm_calls, _prompt_segments

SONNET = "claude-sonnet-4-6"


def test_prefix_is_priced_as_cache_reads_then_writes_then_uncached():
    segments = [["tool_descriptions", 100], ["task_prompt", 100], ["observation:grep_codebase", 200]]
    usage = [{"model": SONNET, "input": 100, "cache_read": 200, "cache_write": 100, "output": 50}]
    out = attribute_call(segments, usage)
    assert out["tool_descriptions"]["cache_read_tokens"] == pytest.approx(100)
    assert out["task_prompt"]["cache_read_tokens"] == pytest.approx(100)
    obs = out["observation:grep_codebase"]
    assert obs["cache_write_tokens"] == pytest.approx(100) and obs["uncached_tokens"] == pytest.approx(100)
    # $3/MTok input: read 0.1x, write 1.25x; $15/MTok output
    assert out["tool_descriptions"]["cost"] == pytest.approx(100 * 3e-6 * 0.1)
    assert obs["cost"] == pytest.approx(100 * 3e-6 * 1.25 + 100 * 3e-6)
    assert out["model_output"]["cost"] == pytest.approx(50 * 15e-6)


def test_char_counts_only_set_proportions_measured_tokens_set_totals():
    out = attribute_call([["a", 10], ["b", 30]],
                         [{"model": SONNET, "input": 400, "cache_read": 0, "cache_write": 0, "output": 0}])
    assert out["a"]["uncached_tokens"] == pytest.approx(100)
    assert out["b"]["uncached_tokens"] == pytest.approx(300)


def test_resends_and_verdict_split():
    usage = [{"model": SONNET, "input": 10, "cache_read": 0, "cache_write": 0, "output": 1}]
    rec = {"verdict": "FAIL", "cost": {"cost_usd": 1.0}, "llm_calls": [
        {"segments": [["task_prompt", 10]], "usage": usage},
        {"segments": [["task_prompt", 10], ["assistant_history", 5], ["observation:get_file_contents", 50]],
         "usage": usage},
        {"segments": [["task_prompt", 10], ["assistant_history", 5], ["observation:get_file_contents", 50],
                      ["assistant_history", 5], ["observation:grep_codebase", 20]], "usage": usage},
    ]}
    rep = analyze([rec])
    assert rep["observation_resends"]["observation:get_file_contents"] == {"observations": 1, "mean_requests_carried": 2.0}
    assert rep["observation_resends"]["observation:grep_codebase"]["mean_requests_carried"] == 1.0
    assert rep["cost_per_case_by_verdict"]["FAIL"]["cases"] == 1
    assert rep["unattributed_cost_usd"] > 0  # recorded $1 vs tiny attributed


def test_prompt_segments_label_each_message_by_the_tool_that_produced_it():
    messages = [
        {"role": "user", "content": "diagnose this"},
        {"role": "assistant", "content": 'Thought: t\nAction: grep_codebase\nAction Input: {"pattern": "x"}'},
        {"role": "user", "content": "Observation: 3 matches"},
        {"role": "assistant", "content": "Thought: done\nAnswer: it is x"},
        {"role": "user", "content": "Observation: REJECTED: call submit_diagnosis"},
    ]
    segs = _prompt_segments(messages, "TOOLS...INSTRUCTIONS", tool_desc_chars=8)
    labels = [s[0] for s in segs]
    assert labels == ["tool_descriptions", "system_instructions", "task_prompt", "assistant_history",
                      "observation:grep_codebase", "assistant_history", "gate_feedback"]
    assert segs[0][1] == 8 and segs[1][1] == len("TOOLS...INSTRUCTIONS") - 8


def test_capture_records_segments_and_the_usage_of_that_call():
    async def complete(messages, system=None, tracing_ctx=None, cache=False):
        cost_meter.record(SONNET, 7, 3, 11, 0)
        return "ok"

    agent = SimpleNamespace(_llm=SimpleNamespace(complete=complete),
                            _tools={"grep_codebase": (None, "abcd")})
    calls: list = []

    async def main():
        with cost_meter.metered() as m:
            _capture_llm_calls(agent, m, calls)
            await agent._llm.complete(messages=[{"role": "user", "content": "hi"}],
                                      system="abcdRULES", tracing_ctx=None, cache=True)

    asyncio.run(main())
    assert calls[0]["segments"][:3] == [["tool_descriptions", 4], ["system_instructions", 5], ["task_prompt", 2]]
    assert calls[0]["usage"] == [{"model": SONNET, "input": 7, "output": 3, "cache_read": 11, "cache_write": 0}]
