"""The harness-directory refactor is behaviour-neutral, and a candidate
harness directory actually changes what DiagnosisAgent sends."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agents.diagnosis import DiagnosisAgent
from app.agents.harness import DEFAULT_ROOT, load_harness
from tests.diagnosis_harness_scenarios import SCENARIOS, capture_static, capture_task_prompt

GOLDEN = Path(__file__).parent / "fixtures" / "diagnosis_harness_golden"


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_task_prompt_is_byte_identical_to_pre_refactor(name):
    assert capture_task_prompt(name) == (GOLDEN / f"task_prompt__{name}.txt").read_text()


def test_system_prompt_tool_descriptions_and_budget_are_unchanged():
    assert capture_static() == json.loads((GOLDEN / "static.json").read_text())


def _agent(harness_dir=None) -> DiagnosisAgent:
    return DiagnosisAgent(github=MagicMock(), local_repo=MagicMock(ready=False, pinned=False),
                          owner="o", repo="r", rag=None, harness_dir=harness_dir)


def _candidate(tmp_path: Path) -> Path:
    cand = tmp_path / "candidate"
    shutil.copytree(DEFAULT_ROOT / "diagnosis", cand)
    return cand


def test_candidate_harness_changes_descriptions_settings_and_prompt(tmp_path):
    cand = _candidate(tmp_path)
    tools = json.loads((cand / "tool_descriptions.json").read_text())
    tools["grep_codebase"] = "Exact-string search. Input: {pattern: string}"
    (cand / "tool_descriptions.json").write_text(json.dumps(tools))
    settings = json.loads((cand / "settings.json").read_text())
    settings["max_iterations"] = 9
    settings["file_read_char_limit"] = 500
    (cand / "settings.json").write_text(json.dumps(settings))
    (cand / "log_context_missing.prompt").write_text("\nNO LOGS.\n")

    agent = _agent(cand)
    assert agent._tools["grep_codebase"][1] == "Exact-string search. Input: {pattern: string}"
    assert agent._max_iterations == 9
    assert agent._harness.setting("file_read_char_limit") == 500
    assert agent._harness.render("log_context_missing") == "\nNO LOGS.\n"


def test_environment_variable_selects_a_candidate(tmp_path, monkeypatch):
    cand = _candidate(tmp_path)
    settings = json.loads((cand / "settings.json").read_text())
    settings["max_iterations"] = 7
    (cand / "settings.json").write_text(json.dumps(settings))
    monkeypatch.setenv("HARNESS_DIR_DIAGNOSIS", str(cand))
    assert _agent()._max_iterations == 7


def test_missing_tool_description_fails_loudly(tmp_path):
    cand = _candidate(tmp_path)
    tools = json.loads((cand / "tool_descriptions.json").read_text())
    del tools["submit_diagnosis"]
    (cand / "tool_descriptions.json").write_text(json.dumps(tools))
    with pytest.raises(KeyError, match="submit_diagnosis"):
        _agent(cand)


def test_settings_doc_keys_are_not_settings():
    h = load_harness("diagnosis")
    assert "_doc" not in h.settings
    assert set(h.settings) == {"max_iterations", "file_read_char_limit",
                               "grep_default_glob", "grep_max_matches"}


def test_dockerignore_does_not_drop_harness_files():
    """The production image is built with .dockerignore applied; a harness file
    it excludes would be missing at runtime (this is why templates are .prompt,
    not .md: .dockerignore drops *.md)."""
    import fnmatch

    root = Path(__file__).resolve().parent.parent
    patterns = [ln.strip() for ln in (root / ".dockerignore").read_text().splitlines()
                if ln.strip() and not ln.strip().startswith(("#", "!"))]
    for f in (DEFAULT_ROOT / "diagnosis").iterdir():
        if f.name == "README.md":        # documentation, never loaded; fine to drop
            continue
        rel = f.relative_to(root).as_posix()
        hits = [p for p in patterns if fnmatch.fnmatch(rel, p) or fnmatch.fnmatch(f.name, p)
                or rel.startswith(p.rstrip("/") + "/")]
        assert not hits, f"{rel} is excluded from the Docker build by {hits}"


def test_search_codebase_is_offered_only_with_a_rag_index():
    from tests.diagnosis_harness_scenarios import capture_task_prompt

    note = (DEFAULT_ROOT / "diagnosis" / "retrieval_unavailable.prompt").read_text()
    without = _agent()                                   # rag=None: production and eval replays today
    assert "search_codebase" not in without._tools
    assert note in capture_task_prompt("no_logs_plain")

    with_rag = DiagnosisAgent(github=MagicMock(), local_repo=MagicMock(ready=False, pinned=False),
                              owner="o", repo="r", rag=MagicMock())
    assert "search_codebase" in with_rag._tools
