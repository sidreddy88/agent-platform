"""fix_with_steps(patch_only=True) returns the fixed file and stops before the
sandbox, the GitHub Issue and the PR (used by scripts/eval_swebench_fix.py)."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agents.fix_generation import FixGenerationAgent
from app.models.events import ErrorEvent, EventSource, IncidentState

CONTENT = "def target(x):\n    return x.value\n"


@pytest.mark.asyncio
async def test_patch_only_returns_the_fixed_file_and_never_touches_github_or_the_sandbox():
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner, agent._repo = "o", "r"
    agent._github = MagicMock()                      # any call on it would be recorded
    agent._local_repo = MagicMock()
    agent._ensure_local_repo = AsyncMock()
    agent._resolve_target = AsyncMock(return_value=("pkg/mod.py", "target"))
    agent._read_file = AsyncMock(return_value=(CONTENT, "sha"))
    agent._fetch_call_chain = AsyncMock(return_value={"callers": [], "tests": [], "imports": []})
    agent._generate_fix = AsyncMock(return_value=(
        "def target(x):\n    return x.value", "def target(x):\n    return x and x.value", [], None))
    agent._critique_fix = AsyncMock(return_value="LOOKS CORRECT")
    incident = IncidentState(error_event=ErrorEvent(source=EventSource.APPLICATION, error_type="TypeError",
                                                    title="x", description="y", service="s"))

    with patch("app.services.sandbox.SandboxService") as sandbox:
        result, steps = await agent.fix_with_steps(incident, patch_only=True)

    assert result.patched_files == {"pkg/mod.py": "def target(x):\n    return x and x.value\n"}
    assert result.files_changed == ["pkg/mod.py"] and result.pr_url is None and result.issue_url is None
    assert steps[-1].startswith("✓ patch_only")
    sandbox.assert_not_called()
    assert not agent._github.method_calls
