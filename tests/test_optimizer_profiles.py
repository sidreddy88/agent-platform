"""Agent profiles: the optimizer's per-agent parts (harness root, setting bounds,
components, grader, health baseline, prompts). Diagnosis stays the default."""
from __future__ import annotations

import shutil

import pytest

from app.agents.harness import DEFAULT_ROOT
from app.harness_optimizer import candidates, fix_grader, profiles, proposer
from app.harness_optimizer.candidates import InvalidCandidate


@pytest.fixture
def fix_profile():
    profiles.use("fix")
    yield profiles.active()
    profiles.use("diagnosis")


def test_default_profile_is_diagnosis():
    p = profiles.active()
    assert p.name == "diagnosis" and p.harness_root == DEFAULT_ROOT / "diagnosis"
    assert "task_prompt" in p.components and "submit_diagnosis" in p.proposer_system


def test_fix_profile_parts(fix_profile):
    p = fix_profile
    assert p.harness_root == DEFAULT_ROOT / "fix" and p.grader is fix_grader
    assert set(p.components) == {"prompts", "skills", "tool_descriptions", "settings"}
    assert "FixGenerationAgent" in p.proposer_system and "FixGenerationAgent" in p.critic_system
    assert set(p.setting_bounds) == set(
        k for k in __import__("json").loads((p.harness_root / "settings.json").read_text()) if not k.startswith("_"))


def test_fix_setting_bounds_are_enforced(fix_profile, tmp_path):
    cand = tmp_path / "fix"
    shutil.copytree(DEFAULT_ROOT / "fix", cand)
    s = (cand / "settings.json").read_text().replace('"max_fix_turns": 20', '"max_fix_turns": 2')
    (cand / "settings.json").write_text(s)
    with pytest.raises(InvalidCandidate, match="max_fix_turns"):
        candidates.validate(DEFAULT_ROOT / "fix", cand)


def test_fix_components_are_validated(fix_profile):
    ok = proposer._one({"component": "skills", "hypothesis": "h",
                        "edits": [{"file": "skills.prompt", "find": "a", "replace": "b"}]})
    assert ok.component == "skills"
    with pytest.raises(InvalidCandidate):
        proposer._one({"component": "task_prompt", "hypothesis": "h",
                       "edits": [{"file": "system.prompt", "find": "a", "replace": "b"}]})


def test_fix_grader_flags_rejections_and_no_patch():
    rec = {"instance_id": "x", "trial": 1, "verdict": "FAIL", "detail": "no patch", "cost": {"cost_usd": 0.02},
           "steps": [{"iteration": 0, "name": "read_file", "input": {"path": "a.py"}, "output": "..."},
                     {"iteration": 1, "name": "read_file", "input": {"path": "a.py"}, "output": "..."},
                     {"iteration": 2, "name": "patch_line", "input": {}, "output": "ERROR: patch_line was NOT recorded: x"},
                     {"iteration": 3, "name": "patch_line", "input": {}, "output": "ERROR: patch_line was NOT recorded: x"}]}
    g = fix_grader.grade(rec)
    assert not g.accepted and g.edits_rejected == 2 and g.repeated_rejections == 1 and g.redundant_calls == 1
    text = fix_grader.render(fix_grader.summarize([g]))
    assert "Ended without a patch: 1/1" in text and "Edits rejected at the call: 2" in text


def test_unknown_profile_is_refused():
    with pytest.raises(ValueError):
        profiles.use("triage")
