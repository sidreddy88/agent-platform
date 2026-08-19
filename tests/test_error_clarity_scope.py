"""
Regression tests for ErrorClarityAgent's observability-only scope enforcement.

Real production bug (VoyageGroupMag/AllInterviews#2595): given a Mongoose "reserved
schema pathname" warning, ErrorClarityAgent used suggest_addition to add
`supressReservedKeysWarning: true` to a schema's options -- a genuine behavior change
(silences the warning) with zero logging/error-handling added. Wrong on two levels:
(1) out of scope regardless of correctness -- this agent's entire mandate, stated in
its own prompt, is "Do NOT fix the bug — only add error visibility," and nothing ever
enforced that; (2) even taken as a fix, it used the grammatically-correct spelling,
not the misspelling this repo's pinned mongoose@6.8.3 actually checks in
lib/schema.js (`this.options.supressReservedKeysWarning`) -- a class of error no
amount of reading the app's own repo could catch. This file tests the fix for (1),
the preventable half.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.error_clarity import ErrorClarityAgent, _adds_observability
from app.models.events import ErrorEvent, EventSource, IncidentState, Severity

# ---------------------------------------------------------------------------
# _adds_observability — the core heuristic
# ---------------------------------------------------------------------------

# The actual before/after from PR #2595, reproduced directly.
REAL_CODE_BEFORE = (
    "  costUsd: { type: Number },\n"
    "  createdAt: { type: Date, default: Date.now }\n"
    "});"
)
REAL_CODE_AFTER = (
    "  costUsd: { type: Number },\n"
    "  createdAt: { type: Date, default: Date.now }\n"
    "}, {\n"
    "  supressReservedKeysWarning: true\n"
    "});"
)


def test_rejects_the_real_schema_option_addition():
    assert _adds_observability(REAL_CODE_BEFORE, REAL_CODE_AFTER) is False


def test_accepts_a_console_error_addition():
    before = "const data = JSON.parse(raw);"
    after = (
        "let data;\n"
        "try {\n"
        "  data = JSON.parse(raw);\n"
        "} catch (err) {\n"
        "  console.error('Failed to parse', err);\n"
        "  throw err;\n"
        "}"
    )
    assert _adds_observability(before, after) is True


def test_accepts_a_catch_clause_addition():
    before = "db.bulkWrite(ops);"
    after = "db.bulkWrite(ops).catch((err) => logger.error('bulkWrite failed', err));"
    assert _adds_observability(before, after) is True


def test_rejects_a_pure_value_change_with_no_logging():
    before = "const RETRY_LIMIT = 3;"
    after = "const RETRY_LIMIT = 5;"
    assert _adds_observability(before, after) is False


def test_does_not_credit_logging_already_present_before():
    """A change that keeps an existing console.error but adds nothing new
    shouldn't count — only NEW observability counts."""
    before = "try { foo(); } catch (e) { console.error(e); }"
    after = "try { foo(); } catch (e) { console.error(e); return null; }"
    assert _adds_observability(before, after) is False


# ---------------------------------------------------------------------------
# Full loop — suggest_addition gets rejected, doesn't reach raw_additions/PR
# ---------------------------------------------------------------------------

def _make_incident() -> IncidentState:
    event = ErrorEvent(
        source=EventSource.CLOUDWATCH,
        title="Reserved schema pathname warning",
        description="`errors` is a reserved schema pathname",
        service="TaskAllInterviews",
        severity=Severity.P3,
    )
    return IncidentState(id="test-incident-1", error_event=event)


def _tool_call(tc_id: str, name: str, inp: dict) -> dict:
    return {"id": tc_id, "name": name, "input": inp}


@pytest.mark.asyncio
async def test_non_observability_addition_is_rejected_and_not_committed():
    agent = ErrorClarityAgent.__new__(ErrorClarityAgent)
    agent._github = MagicMock()
    agent._github.get_file_contents = AsyncMock(return_value=(REAL_CODE_BEFORE, "sha123"))
    agent._llm = MagicMock()
    agent._owner, agent._repo = "owner", "repo"

    responses = [
        (
            "Thought: found it",
            [_tool_call("1", "suggest_addition", {
                "file": "models/PrankCheckerLog.js",
                "function": "(module level)",
                "description": "Suppress the reserved-key warning",
                "code_before": REAL_CODE_BEFORE,
                "code_after": REAL_CODE_AFTER,
            })],
            "tool_use",
        ),
        ("Thought: rejected, giving up", [], "end_turn"),
    ]
    agent._llm.complete_with_tools = AsyncMock(side_effect=responses)

    result = await agent.analyze(_make_incident())

    # Nothing committed — no PR, no additions.
    assert result.additions == []
    assert result.pr_url is None
    agent._github.get_branch_sha = AsyncMock()
    agent._github.get_branch_sha.assert_not_called()


@pytest.mark.asyncio
async def test_rejection_message_tells_agent_to_use_flag_pattern():
    agent = ErrorClarityAgent.__new__(ErrorClarityAgent)
    agent._github = MagicMock()
    agent._llm = MagicMock()
    agent._owner, agent._repo = "owner", "repo"

    responses = [
        (
            "Thought: found it",
            [_tool_call("1", "suggest_addition", {
                "file": "models/PrankCheckerLog.js",
                "function": "(module level)",
                "description": "Suppress the reserved-key warning",
                "code_before": REAL_CODE_BEFORE,
                "code_after": REAL_CODE_AFTER,
            })],
            "tool_use",
        ),
        ("Thought: ok, flagging instead", [], "end_turn"),
    ]
    agent._llm.complete_with_tools = AsyncMock(side_effect=responses)

    await agent.analyze(_make_incident())

    # The tool result fed back to the model must explain why and point at flag_pattern.
    messages = agent._llm.complete_with_tools.call_args_list[1][0][0]
    tool_result = next(m for m in messages if m.get("role") == "tool")
    assert "REJECTED" in tool_result["content"]
    assert "flag_pattern" in tool_result["content"]


@pytest.mark.asyncio
async def test_observability_addition_still_committed_normally():
    """Sanity: a real logging addition isn't caught by this gate — regression guard
    against the heuristic being too aggressive."""
    real_before = "const data = JSON.parse(raw);"
    real_after = (
        "let data;\ntry {\n  data = JSON.parse(raw);\n} catch (err) {\n"
        "  console.error('Failed to parse payload', err);\n  throw err;\n}"
    )
    agent = ErrorClarityAgent.__new__(ErrorClarityAgent)
    agent._github = MagicMock()
    agent._github.get_file_contents = AsyncMock(return_value=(real_before, "sha123"))
    agent._github.get_branch_sha = AsyncMock(return_value="basesha")
    agent._github.create_branch = AsyncMock()
    agent._github.update_file = AsyncMock()
    agent._github.create_pull_request = AsyncMock(return_value=(7, "https://github.com/o/r/pull/7"))
    agent._llm = MagicMock()
    agent._owner, agent._repo = "owner", "repo"

    responses = [
        (
            "Thought: found it",
            [_tool_call("1", "suggest_addition", {
                "file": "routes/api/x.js",
                "function": "parseHandler",
                "description": "Log JSON parse failures",
                "code_before": real_before,
                "code_after": real_after,
            })],
            "tool_use",
        ),
        ("Thought: done", [], "end_turn"),
    ]
    agent._llm.complete_with_tools = AsyncMock(side_effect=responses)

    result = await agent.analyze(_make_incident())

    assert len(result.additions) == 1
    assert result.pr_url == "https://github.com/o/r/pull/7"
