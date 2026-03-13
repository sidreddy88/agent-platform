"""
User preferences loader — reads the ## User Preferences section from CLAUDE.md
and makes the settings available to all agents via a cached singleton.

Agents receive preferences as a prefix in their system prompt so every LLM call
is automatically aware of the user's output style, tone, and risk settings.

Editing CLAUDE.md is the only thing needed to change agent behaviour globally.
The file is re-read on the next server restart (cached for the process lifetime).

Preference keys (all optional — defaults used when absent):
  output_format          concise | detailed | bullet_points   (default: concise)
  tone                   professional | casual | technical     (default: professional)
  always_explain_reasoning  true | false                       (default: true)
  flag_assumptions          true | false                       (default: true)
  risk_threshold         low | medium | high                   (default: medium)
  max_alternatives       integer                               (default: 3)
  currency               e.g. USD, EUR                         (default: USD)
  timezone               e.g. UTC, US/Eastern                  (default: UTC)
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# CLAUDE.md lives at the project root — two levels above this file
_CLAUDE_MD = Path(__file__).parent.parent.parent / "CLAUDE.md"

_DEFAULTS: dict[str, Any] = {
    "output_format":            "concise",
    "tone":                     "professional",
    "always_explain_reasoning": True,
    "flag_assumptions":         True,
    "risk_threshold":           "medium",
    "max_alternatives":         3,
    "currency":                 "USD",
    "timezone":                 "UTC",
}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_preferences(text: str) -> dict[str, Any]:
    """
    Extract key: value pairs from the fenced code block inside the
    '## User Preferences' section of CLAUDE.md.

    Values are lightly typed:
      'true'/'false' → bool
      integers        → int
      everything else → str
    """
    prefs: dict[str, Any] = {}

    # Find the section
    section_match = re.search(
        r"##\s+User Preferences.*?```(.*?)```",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if not section_match:
        return prefs

    block = section_match.group(1)
    for line in block.splitlines():
        # Strip inline comments
        line = re.sub(r"\s*#.*$", "", line).strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key   = key.strip().lower().replace("-", "_")
        value = value.strip()
        if not key or not value:
            continue

        # Type coercion
        if value.lower() == "true":
            prefs[key] = True
        elif value.lower() == "false":
            prefs[key] = False
        elif value.isdigit():
            prefs[key] = int(value)
        else:
            prefs[key] = value

    return prefs


def _load_from_file(path: Path) -> dict[str, Any]:
    """Read CLAUDE.md and return merged preferences (file values override defaults)."""
    if not path.exists():
        logger.debug("[Preferences] %s not found — using defaults", path)
        return dict(_DEFAULTS)

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("[Preferences] Could not read %s: %s — using defaults", path, exc)
        return dict(_DEFAULTS)

    parsed = _parse_preferences(text)
    if not parsed:
        logger.debug("[Preferences] No preferences block found in %s — using defaults", path)

    merged = {**_DEFAULTS, **parsed}
    logger.debug("[Preferences] Loaded: %s", merged)
    return merged


@lru_cache(maxsize=1)
def load_preferences() -> dict[str, Any]:
    """Return merged user preferences (cached for the process lifetime)."""
    return _load_from_file(_CLAUDE_MD)


def clear_cache() -> None:
    """Invalidate the preferences cache — useful in tests."""
    load_preferences.cache_clear()


# ---------------------------------------------------------------------------
# System-prompt builder
# ---------------------------------------------------------------------------

def build_preferences_prompt(prefs: dict[str, Any] | None = None) -> str:
    """
    Return a short system-prompt prefix that communicates user preferences
    to the agent.  Returns an empty string when preferences are all defaults
    and wouldn't meaningfully change behaviour.
    """
    p = prefs if prefs is not None else load_preferences()

    lines: list[str] = ["## User Preferences (apply to all responses)"]

    fmt = p.get("output_format", "concise")
    tone = p.get("tone", "professional")
    lines.append(f"- Tone: {tone}.")

    if fmt == "concise":
        lines.append("- Keep answers concise. Lead with the conclusion. Skip preamble.")
    elif fmt == "bullet_points":
        lines.append("- Format all lists as bullet points. Prefer bullets over prose.")
    elif fmt == "detailed":
        lines.append("- Provide thorough, detailed explanations.")

    if p.get("always_explain_reasoning", True):
        lines.append("- Always include a brief rationale for each recommendation.")

    if p.get("flag_assumptions", True):
        lines.append(
            '- When the input is ambiguous, explicitly state "Assuming X" '
            "before proceeding."
        )

    max_alt = p.get("max_alternatives", 3)
    lines.append(
        f"- When uncertain, suggest up to {max_alt} alternative approaches."
    )

    risk = p.get("risk_threshold", "medium")
    lines.append(
        f"- Flag any action with risk level {risk.upper()} or above before recommending execution."
    )

    currency = p.get("currency", "USD")
    tz = p.get("timezone", "UTC")
    lines.append(f"- Use {currency} for cost estimates. Timestamps in {tz}.")

    return "\n".join(lines)
