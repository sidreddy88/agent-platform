"""
Single source of truth for default Claude model IDs.

Before this module existed, model IDs were hardcoded in two disconnected
places — MODEL/HAIKU_MODEL constants in app/services/llm.py, and a separate
inline fallback string in app/services/llm_gateway.py's _get_routing(). Both
were dated snapshots (e.g. "claude-sonnet-4-20250514"), which Anthropic
eventually retires without warning. That's exactly what happened in Aug
2026: the snapshot pinned in llm.py went dead, silently breaking every agent
built with the default LLMService() (DiagnosisAgent, CodeReviewAgent,
RequirementsAgent, CICDAgent, DeploymentAgent, IncidentResponseAgent,
ErrorClarityAgent, FixGenerationAgent's main call) — invisible because
tracing was also off at the time and the test suite is fully mocked.

Both llm.py and llm_gateway.py now read their default model IDs from here,
which in turn reads config/llm_routing.json's "defaults" section — the same
file that already held the per-task routing table. One file, one place to
update when Anthropic retires a model.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "llm_routing.json"

# Last-resort fallback if config/llm_routing.json is missing/malformed/missing
# the "defaults" key. Keep these pointed at Anthropic's current *rolling*
# aliases (undated, e.g. "claude-sonnet-4-6") rather than a dated snapshot
# (e.g. "claude-sonnet-4-20250514") — dated snapshots are what get retired.
_HARDCODED_FALLBACK = {
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}


def _load_routing_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("[model_config] could not read %s: %s", _CONFIG_PATH, exc)
        return {}


def _load_default_models() -> dict:
    config = _load_routing_config()
    defaults = config.get("defaults", {})
    if "sonnet" in defaults and "haiku" in defaults:
        return defaults
    logger.warning(
        "[model_config] %s has no valid 'defaults' section — using hardcoded "
        "fallback %s. Add a defaults.sonnet/defaults.haiku entry.",
        _CONFIG_PATH, _HARDCODED_FALLBACK,
    )
    return _HARDCODED_FALLBACK


# Read once at import time — mirrors how the old MODEL/HAIKU_MODEL constants
# behaved (module-level constants), just sourced from one file instead of
# being hardcoded independently in two Python modules.
DEFAULT_MODELS = _load_default_models()


async def validate_models_live() -> None:
    """Startup check: confirm every configured Anthropic model ID still exists.

    Dated model snapshots get retired without warning — this is what silently
    broke several agents in Aug 2026 (see module docstring). Runs once at
    FastAPI startup as a background task, logs loudly on a mismatch, and
    never raises — a validation call failing shouldn't take prod down.
    """
    from app.core.config import settings

    if settings.environment == "test" or not settings.anthropic_api_key:
        return

    configured: set[str] = set(DEFAULT_MODELS.values())
    routing_config = _load_routing_config()
    for entry in routing_config.get("routing", {}).values():
        for key in ("model", "fallback_model"):
            model = entry.get(key)
            # Skip non-Anthropic entries (LiteLLM provider-prefixed, e.g.
            # "openai/gpt-4.1") — only Anthropic's own model list applies.
            if model and "/" not in model:
                configured.add(model)

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        page = await client.models.list(limit=100)
        live_ids = {m.id for m in page.data}
    except Exception as exc:
        logger.warning("[model_config] could not verify live model list at startup: %s", exc)
        return

    dead = configured - live_ids
    if dead:
        logger.error(
            "[model_config] %d configured model(s) no longer exist and will 404 on "
            "every call: %s. Update config/llm_routing.json.",
            len(dead), sorted(dead),
        )
    else:
        logger.info(
            "[model_config] all %d configured Anthropic model(s) verified live",
            len(configured),
        )
