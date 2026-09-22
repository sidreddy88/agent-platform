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


def _configured_models() -> tuple[set[str], set[str]]:
    """Every model id in config/llm_routing.json, split by provider.

    Returns (anthropic_ids, openai_ids). Anthropic ids are bare
    ("claude-sonnet-5"); LiteLLM provider-prefixed ids ("openai/gpt-5.5") are
    returned with the prefix stripped, since that is what OpenAI's own model
    list uses. Any other prefix is ignored — there is no list to check it
    against, and guessing would produce false alarms.
    """
    anthropic_ids: set[str] = set(DEFAULT_MODELS.values())
    openai_ids: set[str] = set()
    routing_config = _load_routing_config()
    for entry in routing_config.get("routing", {}).values():
        for key in ("model", "fallback_model"):
            model = entry.get(key)
            if not model:
                continue
            if "/" not in model:
                anthropic_ids.add(model)
            elif model.startswith("openai/"):
                openai_ids.add(model.split("/", 1)[1])
    return anthropic_ids, openai_ids


async def _live_anthropic_ids() -> set[str] | None:
    from app.core.config import settings

    try:
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        page = await client.models.list(limit=100)
        return {m.id for m in page.data}
    except Exception as exc:
        logger.warning("[model_config] could not verify live Anthropic model list: %s", exc)
        return None


async def _live_openai_ids() -> set[str] | None:
    from app.core.config import settings

    if not settings.openai_api_key:
        return None
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        page = await client.models.list()
        return {m.id for m in page.data}
    except Exception as exc:
        logger.warning("[model_config] could not verify live OpenAI model list: %s", exc)
        return None


async def validate_models_live() -> None:
    """Startup check: confirm every configured model id still exists.

    Dated model snapshots get retired without warning — this is what silently
    broke several agents in Aug 2026 (see module docstring). Runs once at
    FastAPI startup as a background task, logs loudly on a mismatch, and
    never raises — a validation call failing shouldn't take prod down.

    Covers OpenAI as well as Anthropic. It originally skipped anything with a
    "/" in it, which meant the review task's model was unverified: a typo'd
    or retired OpenAI id would 404 on every code review with nothing warning
    at startup. That is precisely the silent failure this module exists to
    prevent, so the exemption was the bug.

    Each provider is checked independently — one unreachable list must not
    suppress the other's result.
    """
    from app.core.config import settings

    if settings.environment == "test":
        return

    anthropic_ids, openai_ids = _configured_models()
    checked = 0
    dead: list[str] = []

    if anthropic_ids and settings.anthropic_api_key:
        live = await _live_anthropic_ids()
        if live is not None:
            checked += len(anthropic_ids)
            dead += sorted(anthropic_ids - live)

    if openai_ids:
        live = await _live_openai_ids()
        if live is not None:
            checked += len(openai_ids)
            dead += sorted(f"openai/{m}" for m in openai_ids - live)

    if dead:
        logger.error(
            "[model_config] %d configured model(s) no longer exist and will 404 on "
            "every call: %s. Update config/llm_routing.json.",
            len(dead), dead,
        )
    elif checked:
        logger.info("[model_config] all %d configured model(s) verified live", checked)
