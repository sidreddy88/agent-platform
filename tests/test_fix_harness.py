"""
FixGenerationAgent's prompts, tool descriptions and loop settings live in
app/agents/harness/fix/ (so the harness optimizer can edit them). Moving them
there must not change what production sends: the rendered requests are compared
with a snapshot taken from the hardcoded version (tests/fixtures/fix_prompt_snapshot.json).
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.agents import fix_generation
from app.agents.harness import DEFAULT_ROOT, load_harness
from tests._fix_prompt_capture import capture_all

SNAPSHOT = json.loads((Path(__file__).parent / "fixtures" / "fix_prompt_snapshot.json").read_text())


def test_rendered_requests_match_the_pre_refactor_snapshot(monkeypatch):
    monkeypatch.delenv("HARNESS_DIR_FIX", raising=False)
    assert json.loads(json.dumps(capture_all())) == SNAPSHOT


def test_default_settings_equal_the_module_constants():
    h = load_harness("fix")
    assert h.setting("max_fix_turns") == fix_generation._MAX_FIX_TURNS
    assert h.setting("budget_warning_turns") == fix_generation._BUDGET_WARNING_TURNS
    assert h.setting("no_edit_nudges") == fix_generation._NO_EDIT_NUDGES
    assert h.setting("max_cutoffs") == fix_generation._MAX_CUTOFFS


def test_a_candidate_harness_changes_the_request(tmp_path, monkeypatch):
    cand = tmp_path / "fix"
    shutil.copytree(DEFAULT_ROOT / "fix", cand)
    (cand / "skills.prompt").write_text("LESSONS:\n- Check sibling methods after fixing one.\n")
    (cand / "system.prompt").write_text("You fix bugs carefully.")
    monkeypatch.setenv("HARNESS_DIR_FIX", str(cand))
    calls = capture_all()["function"]["calls"]
    assert calls[0]["system"] == "You fix bugs carefully."
    assert "Check sibling methods after fixing one." in calls[0]["last"]


def test_skills_prompt_is_empty_by_default():
    assert load_harness("fix").render("skills") == ""
