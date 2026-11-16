"""The parser's boundary: whatever the model says, only known apps, real future dates, and questions for the rest."""
from app.agents.identity.request_parser import validate

APPS = {"figma", "aws-admin", "tableau"}


def test_unknown_apps_become_questions_and_never_access():
    out = validate({"extra_apps": ["figma", "photoshop"]}, APPS, "2026-11-16")
    assert out["extra_apps"] == ["figma"] and "photoshop" in out["questions"][0]


def test_a_past_or_malformed_date_is_a_question_not_a_date():
    assert validate({"start_date": "2026-11-15"}, APPS, "2026-11-16")["start_date"] is None
    assert validate({"start_date": "the 15th"}, APPS, "2026-11-16")["start_date"] is None
    assert validate({"start_date": "2026-12-15"}, APPS, "2026-11-16")["start_date"] == "2026-12-15"


def test_unexpected_keys_and_shapes_are_dropped():
    out = validate({"run_command": "rm -rf /", "extra_groups": ["Design Team"], "copy_access_from": " Bob "}, APPS,
                   "2026-11-16")
    assert "run_command" not in out and out["extra_groups"] == ["design-team"] and out["copy_access_from"] == "bob"
