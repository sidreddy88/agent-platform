"""The shipped triage harness renders exactly the pinned prompts, and a candidate harness directory actually
changes what TriageAgent sends.

The golden files were captured from triage.py before its prompt moved into app/agents/harness/triage/, so
they prove the move was byte-identical. From now on they pin the shipped harness: a deliberate harness change
updates them in the same PR (python -m tests.triage_harness_scenarios), and the diff shows exactly what the
model will be sent differently."""
from __future__ import annotations

import fnmatch
import json
import shutil
from pathlib import Path

import pytest

from app.agents.harness import DEFAULT_ROOT, load_harness
from tests.triage_harness_scenarios import SCENARIOS, _agent, capture_static, capture_task_prompt

GOLDEN = Path(__file__).parent / "fixtures" / "triage_harness_golden"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_task_prompt_matches_pinned_golden(name):
    assert capture_task_prompt(name) == (GOLDEN / f"task_prompt__{name}.txt").read_text()


def test_system_prompt_tool_descriptions_and_budget_are_unchanged():
    assert capture_static() == json.loads((GOLDEN / "static.json").read_text())


def _candidate(tmp_path: Path) -> Path:
    cand = tmp_path / "candidate"
    shutil.copytree(DEFAULT_ROOT / "triage", cand)
    return cand


def test_candidate_harness_changes_descriptions_settings_and_prompt(tmp_path):
    cand = _candidate(tmp_path)
    tools = json.loads((cand / "tool_descriptions.json").read_text())
    tools["get_occurrence_count"] = "ALWAYS call this. Input: {log_group: string, pattern: string}"
    (cand / "tool_descriptions.json").write_text(json.dumps(tools))
    settings = json.loads((cand / "settings.json").read_text())
    settings["max_iterations"] = 6
    (cand / "settings.json").write_text(json.dumps(settings))
    (cand / "occurrence_no_log_group.prompt").write_text("2. No log group: say occurrences are unknown")

    agent = _agent(cand)
    assert agent._tools["get_occurrence_count"][1].startswith("ALWAYS call this.")
    assert agent._max_iterations == 6
    assert "2. No log group: say occurrences are unknown" in capture_task_prompt("no_log_group", cand)


def test_environment_variable_selects_a_candidate(tmp_path, monkeypatch):
    cand = _candidate(tmp_path)
    settings = json.loads((cand / "settings.json").read_text())
    settings["max_iterations"] = 7
    (cand / "settings.json").write_text(json.dumps(settings))
    monkeypatch.setenv("HARNESS_DIR_TRIAGE", str(cand))
    assert _agent()._max_iterations == 7


def test_missing_tool_description_fails_loudly(tmp_path):
    cand = _candidate(tmp_path)
    tools = json.loads((cand / "tool_descriptions.json").read_text())
    del tools["check_duplicate_pr"]
    (cand / "tool_descriptions.json").write_text(json.dumps(tools))
    with pytest.raises(KeyError, match="check_duplicate_pr"):
        _agent(cand)


def test_settings_doc_keys_are_not_settings():
    assert set(load_harness("triage").settings) == {"max_iterations"}


def test_dockerignore_does_not_drop_harness_files():
    root = Path(__file__).resolve().parent.parent
    patterns = [ln.strip() for ln in (root / ".dockerignore").read_text().splitlines()
                if ln.strip() and not ln.strip().startswith(("#", "!"))]
    for f in (DEFAULT_ROOT / "triage").iterdir():
        if f.name == "README.md":        # documentation, never loaded; fine to drop
            continue
        rel = f.relative_to(root).as_posix()
        hits = [p for p in patterns if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(f.name, p)
                or rel.startswith(p.rstrip("/") + "/")]
        assert not hits, f"{rel} is excluded from the Docker build by {hits}"


def test_gate_baseline_hash_covers_every_harness_file():
    """The triage gate hashes its inputs to tell whether the baseline is stale; a harness file it doesn't hash
    could change triage's behaviour without the gate noticing."""
    from scripts.triage_gate import INPUTS

    root = Path(__file__).resolve().parent.parent
    shipped = {f.relative_to(root).as_posix() for f in (DEFAULT_ROOT / "triage").iterdir() if f.name != "README.md"}
    assert shipped <= set(INPUTS)
