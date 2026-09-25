"""LLM-rubric grader, label sheet round trip, and agreement stats."""
from __future__ import annotations

import asyncio
import json

import pytest

from app.harness_optimizer.rubric import QUESTIONS, agreement, judge, render

REC = {"instance_id": "c1", "trial": 1, "verdict": "FAIL", "steps": [
    {"iteration": 1, "name": "grep_codebase", "input": '{"pattern": "foo"}',
     "output": "src/a.py:3: def foo():", "thought": "foo is the suspect"}]}


def test_render_shows_reasoning_calls_and_results():
    text = render(REC)
    assert "grep_codebase" in text and "foo is the suspect" in text and "src/a.py:3" in text


def test_judge_accepts_fenced_json_and_rejects_incomplete_answers():
    full = {q: "yes" for q in QUESTIONS}

    async def good(system, prompt):
        return "```json\n" + json.dumps({"answers": full, "reasons": {}}) + "\n```"

    async def partial(system, prompt):
        return json.dumps({"answers": {"hypothesis_early": "yes"}})

    assert asyncio.run(judge(REC, good))["answers"] == full
    with pytest.raises(ValueError, match="no valid answers"):
        asyncio.run(judge(REC, partial))


def test_agreement_and_kappa():
    qs = list(QUESTIONS)
    human = {"t1": {q: "yes" for q in qs}, "t2": {q: "no" for q in qs}}
    same = agreement(human, human)
    assert same["agreement"] == 1.0 and same["cohens_kappa"] == 1.0
    flipped = {"t1": {q: "no" for q in qs}, "t2": {q: "yes" for q in qs}}
    assert agreement(human, flipped)["agreement"] == 0.0
    assert agreement(human, {})["compared"] == 0


def test_label_sheet_round_trip(tmp_path):
    from scripts.rubric_label_sheet import make, parse

    run = tmp_path / "run" / "evals" / "h" / "k2"
    run.mkdir(parents=True)
    (run / "c1.json").write_text(json.dumps({"case": {}, "trajectories": [
        {**REC, "trial": 1}, {**REC, "trial": 2, "verdict": "PASS"}]}))
    sheet = tmp_path / "labels.md"
    make(tmp_path / "run", 2, sheet)
    text = sheet.read_text()
    assert "## h/c1/t1" in text and text.count("answer: ") == 2 * len(QUESTIONS)
    filled = text.replace("answer: ", "answer: yes", len(QUESTIONS))    # label the first trajectory only
    sheet.write_text(filled)
    out = tmp_path / "labels.jsonl"
    parse(sheet, out)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    complete = [r for r in rows if len(r["answers"]) == len(QUESTIONS)]
    assert len(complete) == 1 and set(complete[0]["answers"].values()) == {"yes"}
