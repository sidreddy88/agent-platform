"""FIX_SELF_CRITIQUE=off skips the Haiku self-critique (and so its alternate-frame
retry). On 346 graded SWE-bench patches it was no better than chance; it stays on
by default because it was built for production incidents with stack traces."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents import fix_generation
from app.agents.fix_generation import FixGenerationAgent
from app.models.events import ErrorEvent, EventSource, IncidentState

CONTENT = "def target(x):\n    return x.value\n"


def _agent():
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner, agent._repo = "o", "r"
    agent._github = MagicMock()
    agent._local_repo = MagicMock()
    agent._ensure_local_repo = AsyncMock()
    agent._resolve_target = AsyncMock(return_value=("pkg/mod.py", "target"))
    agent._read_file = AsyncMock(return_value=(CONTENT, "sha"))
    agent._fetch_call_chain = AsyncMock(return_value={"callers": [], "tests": [], "imports": []})
    agent._generate_fix = AsyncMock(return_value=(
        "def target(x):\n    return x.value", "def target(x):\n    return x and x.value", [], None))
    agent._critique_fix = AsyncMock(return_value="LIKELY WRONG: guards the symptom")
    return agent


async def _run(agent):
    incident = IncidentState(error_event=ErrorEvent(source=EventSource.APPLICATION, error_type="TypeError",
                                                    title="x", description="y", service="s"))
    with patch("app.services.sandbox.SandboxService"):
        return await agent.fix_with_steps(incident, patch_only=True)


@pytest.mark.asyncio
async def test_off_skips_the_critique(monkeypatch):
    monkeypatch.setenv("FIX_SELF_CRITIQUE", "off")
    agent = _agent()
    result, steps = await _run(agent)
    agent._critique_fix.assert_not_called()
    assert "– Self-critique skipped (FIX_SELF_CRITIQUE=off)" in steps
    assert not any("LIKELY WRONG" in s for s in steps)
    assert result.patched_files == {"pkg/mod.py": "def target(x):\n    return x and x.value\n"}


@pytest.mark.asyncio
async def test_default_still_runs_the_critique(monkeypatch):
    monkeypatch.delenv("FIX_SELF_CRITIQUE", raising=False)
    agent = _agent()
    _, steps = await _run(agent)
    agent._critique_fix.assert_called_once()
    assert any(s.startswith("✓ Self-critique:") for s in steps)


@pytest.mark.parametrize("value,enabled", [("off", False), ("0", False), ("False", False), ("no", False),
                                           ("on", True), ("1", True), ("", True)])
def test_switch_values(monkeypatch, value, enabled):
    monkeypatch.setenv("FIX_SELF_CRITIQUE", value)
    assert fix_generation._self_critique_enabled() is enabled
