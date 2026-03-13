"""
Tests for the preferences loader and system-prompt builder.

Run:
    pytest tests/test_preferences.py -v
"""

import pytest

from app.services.preferences import (
    _DEFAULTS,
    _parse_preferences,
    build_preferences_prompt,
    clear_cache,
    load_preferences,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_CLAUDE_MD = """
# Agent Platform

Some project docs here.

## User Preferences

> Agents read this section before every run.

```
output_format: bullet_points
tone: casual
always_explain_reasoning: false
flag_assumptions: true
risk_threshold: high
max_alternatives: 5
currency: EUR
timezone: US/Eastern
```

## Another Section

Ignored content.
"""

MINIMAL_CLAUDE_MD = """
## User Preferences

```
tone: technical
```
"""

NO_PREFERENCES_CLAUDE_MD = """
# Project

No preferences section here.
"""


# ---------------------------------------------------------------------------
# _parse_preferences
# ---------------------------------------------------------------------------

class TestParsePreferences:
    def test_parses_all_keys(self):
        prefs = _parse_preferences(SAMPLE_CLAUDE_MD)
        assert prefs["output_format"] == "bullet_points"
        assert prefs["tone"] == "casual"
        assert prefs["always_explain_reasoning"] is False
        assert prefs["flag_assumptions"] is True
        assert prefs["risk_threshold"] == "high"
        assert prefs["max_alternatives"] == 5
        assert prefs["currency"] == "EUR"
        assert prefs["timezone"] == "US/Eastern"

    def test_strips_inline_comments(self):
        md = "## User Preferences\n```\ntone: casual  # this is a comment\n```"
        prefs = _parse_preferences(md)
        assert prefs["tone"] == "casual"

    def test_bool_true_coercion(self):
        md = "## User Preferences\n```\nflag_assumptions: true\n```"
        prefs = _parse_preferences(md)
        assert prefs["flag_assumptions"] is True

    def test_bool_false_coercion(self):
        md = "## User Preferences\n```\nalways_explain_reasoning: false\n```"
        prefs = _parse_preferences(md)
        assert prefs["always_explain_reasoning"] is False

    def test_integer_coercion(self):
        md = "## User Preferences\n```\nmax_alternatives: 7\n```"
        prefs = _parse_preferences(md)
        assert prefs["max_alternatives"] == 7
        assert isinstance(prefs["max_alternatives"], int)

    def test_string_value_kept_as_str(self):
        md = "## User Preferences\n```\ncurrency: GBP\n```"
        prefs = _parse_preferences(md)
        assert prefs["currency"] == "GBP"

    def test_no_preferences_section_returns_empty(self):
        prefs = _parse_preferences(NO_PREFERENCES_CLAUDE_MD)
        assert prefs == {}

    def test_empty_string_returns_empty(self):
        prefs = _parse_preferences("")
        assert prefs == {}

    def test_keys_normalised_to_lowercase(self):
        md = "## User Preferences\n```\nOutput_Format: detailed\n```"
        prefs = _parse_preferences(md)
        assert "output_format" in prefs

    def test_hyphens_converted_to_underscores(self):
        md = "## User Preferences\n```\nalways-explain-reasoning: true\n```"
        prefs = _parse_preferences(md)
        assert "always_explain_reasoning" in prefs


# ---------------------------------------------------------------------------
# load_preferences (with cache cleared between tests)
# ---------------------------------------------------------------------------

class TestLoadPreferences:
    def setup_method(self):
        clear_cache()

    def test_returns_defaults_when_no_file(self, tmp_path, monkeypatch):
        import app.services.preferences as pref_mod
        monkeypatch.setattr(pref_mod, "_CLAUDE_MD", tmp_path / "nonexistent.md")
        clear_cache()
        prefs = load_preferences()
        assert prefs == _DEFAULTS

    def test_file_values_override_defaults(self, tmp_path, monkeypatch):
        import app.services.preferences as pref_mod
        md = tmp_path / "CLAUDE.md"
        md.write_text(SAMPLE_CLAUDE_MD)
        monkeypatch.setattr(pref_mod, "_CLAUDE_MD", md)
        clear_cache()

        prefs = load_preferences()
        assert prefs["tone"] == "casual"
        assert prefs["currency"] == "EUR"

    def test_missing_keys_filled_with_defaults(self, tmp_path, monkeypatch):
        import app.services.preferences as pref_mod
        md = tmp_path / "CLAUDE.md"
        md.write_text(MINIMAL_CLAUDE_MD)
        monkeypatch.setattr(pref_mod, "_CLAUDE_MD", md)
        clear_cache()

        prefs = load_preferences()
        assert prefs["tone"] == "technical"             # from file
        assert prefs["currency"] == _DEFAULTS["currency"]  # default

    def test_result_is_cached(self, tmp_path, monkeypatch):
        import app.services.preferences as pref_mod
        md = tmp_path / "CLAUDE.md"
        md.write_text(MINIMAL_CLAUDE_MD)
        monkeypatch.setattr(pref_mod, "_CLAUDE_MD", md)
        clear_cache()

        p1 = load_preferences()
        p2 = load_preferences()
        assert p1 is p2  # same object — cached

    def test_clear_cache_forces_reload(self, tmp_path, monkeypatch):
        import app.services.preferences as pref_mod
        md = tmp_path / "CLAUDE.md"
        md.write_text(MINIMAL_CLAUDE_MD)
        monkeypatch.setattr(pref_mod, "_CLAUDE_MD", md)
        clear_cache()

        p1 = load_preferences()
        clear_cache()
        p2 = load_preferences()
        assert p1 == p2   # same values, but different objects after reload
        assert p1 is not p2


# ---------------------------------------------------------------------------
# build_preferences_prompt
# ---------------------------------------------------------------------------

class TestBuildPreferencesPrompt:
    def test_contains_user_preferences_header(self):
        prompt = build_preferences_prompt()
        assert "## User Preferences" in prompt

    def test_concise_format_adds_concise_instruction(self):
        prompt = build_preferences_prompt({"output_format": "concise", **_DEFAULTS})
        assert "concise" in prompt.lower()

    def test_bullet_points_format_adds_bullet_instruction(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "output_format": "bullet_points"})
        assert "bullet" in prompt.lower()

    def test_detailed_format_adds_detailed_instruction(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "output_format": "detailed"})
        assert "detailed" in prompt.lower() or "thorough" in prompt.lower()

    def test_tone_included(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "tone": "casual"})
        assert "casual" in prompt

    def test_explain_reasoning_true_adds_rationale_instruction(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "always_explain_reasoning": True})
        assert "rationale" in prompt.lower() or "reasoning" in prompt.lower()

    def test_explain_reasoning_false_skips_rationale(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "always_explain_reasoning": False})
        assert "rationale" not in prompt.lower()

    def test_flag_assumptions_true_adds_instruction(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "flag_assumptions": True})
        assert "Assuming" in prompt or "assum" in prompt.lower()

    def test_flag_assumptions_false_skips_instruction(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "flag_assumptions": False})
        assert "Assuming X" not in prompt

    def test_max_alternatives_reflected_in_prompt(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "max_alternatives": 5})
        assert "5" in prompt

    def test_risk_threshold_reflected_in_prompt(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "risk_threshold": "high"})
        assert "HIGH" in prompt

    def test_currency_reflected_in_prompt(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "currency": "EUR"})
        assert "EUR" in prompt

    def test_timezone_reflected_in_prompt(self):
        prompt = build_preferences_prompt({**_DEFAULTS, "timezone": "US/Eastern"})
        assert "US/Eastern" in prompt


# ---------------------------------------------------------------------------
# Integration: preferences appear in BaseAgent system prompt
# ---------------------------------------------------------------------------

class TestPreferencesInBaseAgent:
    def test_system_prompt_contains_user_preferences_header(self):
        from app.agents.base import _build_system_prompt
        prompt = _build_system_prompt({})
        assert "## User Preferences" in prompt

    def test_system_prompt_with_tools_contains_preferences_and_react_format(self):
        from app.agents.base import _build_system_prompt
        from unittest.mock import AsyncMock
        tools = {"search": (AsyncMock(), "Search for something")}
        prompt = _build_system_prompt(tools)
        assert "## User Preferences" in prompt
        assert "TOOLS AVAILABLE" in prompt
        assert "search" in prompt

    def test_preferences_come_before_tools(self):
        from app.agents.base import _build_system_prompt
        from unittest.mock import AsyncMock
        tools = {"mytool": (AsyncMock(), "Does something")}
        prompt = _build_system_prompt(tools)
        pref_idx = prompt.index("## User Preferences")
        tools_idx = prompt.index("TOOLS AVAILABLE")
        assert pref_idx < tools_idx
