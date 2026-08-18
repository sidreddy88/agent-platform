"""
Tests for the FixGenerationAgent tiered context layout (Tier 1 / 2 / 3),
diagnosis blast_radius consumption, and contract-change banner.

Runs against the agent's pure helpers — no LLM calls.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.fix_generation import FixGenerationAgent
from app.models.events import ErrorEvent, EventSource, IncidentState


def _make_agent() -> FixGenerationAgent:
    """Bare agent — bypass __init__ since we don't need LLM/GitHub for these tests."""
    agent = FixGenerationAgent.__new__(FixGenerationAgent)
    agent._owner = "owner"
    agent._repo = "repo"
    agent._github = MagicMock()
    agent._llm = MagicMock()
    return agent


def _make_incident(**overrides) -> IncidentState:
    event = ErrorEvent(
        source=EventSource.APPLICATION,
        error_type="TypeError",
        title="x",
        description="y",
        service="svc",
    )
    inc = IncidentState(error_event=event)
    for k, v in overrides.items():
        setattr(inc, k, v)
    return inc


# ---------------------------------------------------------------------------
# _format_blast_radius
# ---------------------------------------------------------------------------

def test_format_blast_radius_renders_entries():
    agent = _make_agent()
    out = agent._format_blast_radius([
        {"file": "src/api.js", "function": "submitOrder", "snippet": "await processOrder(payload)"},
        {"file": "src/cron.js", "function": "retryFailed", "snippet": "processOrder(...)"},
    ])
    assert "src/api.js :: submitOrder" in out
    assert "await processOrder(payload)" in out
    assert "src/cron.js :: retryFailed" in out


def test_format_blast_radius_skips_entries_without_file():
    agent = _make_agent()
    out = agent._format_blast_radius([
        {"function": "callerA"},
        {"file": "good.js", "function": "callerB", "snippet": ""},
    ])
    assert "good.js :: callerB" in out
    assert "callerA" not in out


def test_format_blast_radius_returns_empty_for_no_entries():
    agent = _make_agent()
    assert agent._format_blast_radius([]) == ""
    assert agent._format_blast_radius(None or []) == ""  # type: ignore


# ---------------------------------------------------------------------------
# _format_tier_block
# ---------------------------------------------------------------------------

def test_format_tier_block_renders_each_path():
    agent = _make_agent()
    out = agent._format_tier_block("CALLER", [
        ("src/a.js", "code A"),
        ("src/b.js", "code B"),
    ])
    assert "--- CALLER: src/a.js ---" in out
    assert "code A" in out
    assert "--- CALLER: src/b.js ---" in out
    assert "code B" in out


def test_format_tier_block_caps_per_item_content():
    agent = _make_agent()
    huge = "X" * 5000
    out = agent._format_tier_block("IMPORT", [("big.js", huge)], per_item_cap=100)
    assert out.count("X") == 100  # capped


# ---------------------------------------------------------------------------
# _generate_fix prompt assembly — tier layout + contract warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_uses_tiered_layout(monkeypatch):
    """The assembled prompt must label TIER 1 / 2 / 3 sections explicitly."""
    agent = _make_agent()

    captured_prompt: dict[str, str] = {}

    async def fake_complete_with_tools(messages=None, tools=None, system=None, **kwargs):
        # First user message carries the assembled prompt; capture it then exit the loop.
        if messages:
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "user":
                    captured_prompt["text"] = m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content"))
                    break
        # Return (text, tool_calls, stop_reason) — empty tool_calls + end_turn ends the loop.
        return ("", [], "end_turn")

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake_complete_with_tools)
    agent._with_harness = MagicMock(return_value="(harness)")

    incident = _make_incident(
        diagnosis="root cause text",
        diagnosis_blast_radius=[
            {"file": "src/api.js", "function": "submitOrder", "snippet": "processOrder()"},
        ],
        diagnosis_contract_change="signature",
        diagnosis_contract_change_detail="added retries param",
    )

    bundle = {
        "callers": [],   # diagnosis blast_radius takes priority
        "tests": [("tests/orders.test.js", "describe('orders')")],
        "imports": [("src/utils.js", "export function helper() {}")],
    }

    try:
        await agent._generate_fix(
            content="function processOrder() {}",
            function_name="processOrder",
            incident=incident,
            file_path="src/orders.js",
            context_bundle=bundle,
        )
    except Exception:
        # We don't care if the LLM stub causes a downstream parse failure;
        # we only need to capture the prompt text.
        pass

    text = captured_prompt.get("text", "")
    # Tier headers
    assert "## TIER 1 — Code you are changing" in text
    assert "## TIER 2 — Callers, tests, and type contracts your fix MUST NOT BREAK" in text
    assert "## TIER 3 — Background context" in text

    # Diagnosis blast_radius wins over fallback callers (none provided here anyway).
    assert "src/api.js :: submitOrder" in text
    # Tests render in Tier 2.
    assert "TEST: tests/orders.test.js" in text
    # Imports render in Tier 3.
    assert "IMPORT: src/utils.js" in text

    # Contract-change banner appears at the top, with detail.
    assert "CONTRACT CHANGE" in text
    assert "signature" in text
    assert "added retries param" in text


@pytest.mark.asyncio
async def test_prompt_omits_contract_warning_when_none(monkeypatch):
    """No contract change → no banner."""
    agent = _make_agent()
    captured_prompt: dict[str, str] = {}

    async def fake_complete_with_tools(messages=None, tools=None, system=None, **kwargs):
        if messages:
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "user":
                    captured_prompt["text"] = m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content"))
                    break
        return ("", [], "end_turn")

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake_complete_with_tools)
    agent._with_harness = MagicMock(return_value="(harness)")

    incident = _make_incident(
        diagnosis="x",
        diagnosis_contract_change="none",
    )

    try:
        await agent._generate_fix(
            content="fn()",
            function_name="fn",
            incident=incident,
            file_path="src/x.js",
            context_bundle={"callers": [], "tests": [], "imports": []},
        )
    except Exception:
        pass

    assert "CONTRACT CHANGE" not in captured_prompt.get("text", "")


# ---------------------------------------------------------------------------
# _critique_fix — broadened four-check prompt
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_critique_includes_four_checks_and_blast_radius():
    """The critique prompt names the four checks and surfaces Tier 2 callers."""
    agent = _make_agent()
    captured = {"prompt": ""}

    async def fake_complete(messages=None, system=None, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return "Looks fine.\nLOOKS CORRECT"

    agent._llm_haiku = MagicMock()
    agent._llm_haiku.complete = AsyncMock(side_effect=fake_complete)
    agent._rag = None
    agent._with_harness = MagicMock(return_value="(harness)")

    incident = _make_incident(
        diagnosis="x",
        diagnosis_blast_radius=[
            {"file": "src/api.js", "function": "submitOrder", "snippet": "processOrder()"},
        ],
        diagnosis_contract_change="signature",
        diagnosis_contract_change_detail="added retries param",
    )

    out = await agent._critique_fix("function processOrder() {}", "function processOrder(retries=0) {}", incident, "src/orders.js")

    text = captured["prompt"]
    # Four checks each present.
    assert "Does the fix BREAK any Tier 2 caller" in text
    assert "edge case" in text.lower()
    assert "SIMPLER fix" in text
    assert "Does any other file need updating" in text
    # Tier 2 surface.
    assert "TIER 2 CALLERS" in text
    assert "src/api.js :: submitOrder" in text
    # Contract change surface.
    assert "CONTRACT CHANGE" in text
    assert "added retries param" in text
    # Symptom-fix checklist still present (load-bearing).
    assert "SYMPTOM-FIX CHECKLIST" in text
    # Verdict line still in output.
    assert "LOOKS CORRECT" in out


@pytest.mark.asyncio
async def test_critique_omits_blast_radius_section_when_empty():
    """No Tier 2 callers → no TIER 2 block in the critique prompt."""
    agent = _make_agent()
    captured = {"prompt": ""}

    async def fake_complete(messages=None, system=None, **kwargs):
        captured["prompt"] = messages[0]["content"]
        return "ok"

    agent._llm_haiku = MagicMock()
    agent._llm_haiku.complete = AsyncMock(side_effect=fake_complete)
    agent._rag = None
    agent._with_harness = MagicMock(return_value="(harness)")

    incident = _make_incident(diagnosis="x")  # no blast_radius, no contract_change

    await agent._critique_fix("old", "new", incident, "src/x.js")

    text = captured["prompt"]
    assert "TIER 2 CALLERS" not in text
    assert "CONTRACT CHANGE" not in text


# ---------------------------------------------------------------------------
# FixResult — confidence/escalate persistence
# ---------------------------------------------------------------------------

def test_fix_result_defaults_for_verdict_fields():
    """New verdict fields default to None / False so existing call sites keep working."""
    from app.agents.fix_generation import FixResult

    r = FixResult(
        issue_url=None, pr_url="x", pr_number=1, branch="b", fix_description="d",
    )
    assert r.confidence is None
    assert r.escalate is False
    assert r.escalate_reason is None
    assert r.blast_radius_addressed is None


def test_fix_result_round_trips_verdict_fields():
    from app.agents.fix_generation import FixResult

    r = FixResult(
        issue_url=None, pr_url="x", pr_number=1, branch="b", fix_description="d",
        confidence=0.62, escalate=True, escalate_reason="unsure about retries flag",
        blast_radius_addressed=False,
    )
    assert r.confidence == 0.62
    assert r.escalate is True
    assert r.escalate_reason == "unsure about retries flag"
    assert r.blast_radius_addressed is False


@pytest.mark.asyncio
async def test_prompt_falls_back_to_search_callers_when_blast_radius_empty(monkeypatch):
    """Empty diagnosis blast_radius → caller search results populate Tier 2."""
    agent = _make_agent()
    captured_prompt: dict[str, str] = {}

    async def fake_complete_with_tools(messages=None, tools=None, system=None, **kwargs):
        if messages:
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "user":
                    captured_prompt["text"] = m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content"))
                    break
        return ("", [], "end_turn")

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake_complete_with_tools)
    agent._with_harness = MagicMock(return_value="(harness)")

    incident = _make_incident(
        diagnosis="x",
        diagnosis_blast_radius=[],   # nothing from diagnosis
    )

    bundle = {
        "callers": [("src/searched_caller.js", "fallback caller body")],
        "tests": [],
        "imports": [],
    }

    try:
        await agent._generate_fix(
            content="fn()",
            function_name="fn",
            incident=incident,
            file_path="src/x.js",
            context_bundle=bundle,
        )
    except Exception:
        pass

    text = captured_prompt.get("text", "")
    assert "TIER 2" in text
    assert "src/searched_caller.js" in text


# ---------------------------------------------------------------------------
# additional_fix_section — must not invite exploring files this call can't edit
# ---------------------------------------------------------------------------

async def _capture_prompt(agent, incident, **kwargs):
    """Run _generate_fix with a stub LLM that ends the turn immediately, return the prompt."""
    captured_prompt: dict[str, str] = {}

    async def fake_complete_with_tools(messages=None, tools=None, system=None, **_kwargs):
        if messages:
            for m in messages:
                if isinstance(m, dict) and m.get("role") == "user":
                    captured_prompt["text"] = (
                        m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content"))
                    )
                    break
        return ("", [], "end_turn")

    agent._llm = MagicMock()
    agent._llm.complete_with_tools = AsyncMock(side_effect=fake_complete_with_tools)
    agent._with_harness = MagicMock(return_value="(harness)")

    try:
        await agent._generate_fix(
            content=kwargs.pop("content", "function target() {}"),
            function_name=kwargs.pop("function_name", "target"),
            incident=incident,
            file_path=kwargs.pop("file_path", "src/target.js"),
            context_bundle=kwargs.pop("context_bundle", {"callers": [], "tests": [], "imports": []}),
            **kwargs,
        )
    except Exception:
        pass
    return captured_prompt.get("text", "")


@pytest.mark.asyncio
async def test_additional_fix_section_forbids_reading_other_files_with_secondary_target():
    """When a single secondary file IS structurally supported, the prompt must still
    forbid exploring it here — it gets its own dedicated _generate_fix() pass in run()."""
    agent = _make_agent()
    incident = _make_incident(
        diagnosis_additional_fix=(
            "Apply the identical fix to all 7 remaining sibling files: shoutout.js, cr.js, "
            "boldJourney.js, artistOfTheDay.js, cityNational.js, highlightApp.js, smallBiz.js"
        ),
        diagnosis_additional_fix_file="routes/api/shoutoutInterviewUsers.js",
        diagnosis_additional_fix_function="(anonymous route handler)",
    )

    text = await _capture_prompt(agent, incident)

    assert "SECONDARY FIX NEEDED" not in text  # old wording that invited exploration
    assert "fixed automatically in a separate pass" in text
    assert "Do NOT call read_file on any file other than src/target.js" in text
    assert "shoutoutInterviewUsers.js" in text  # still surfaced, just as background


@pytest.mark.asyncio
async def test_additional_fix_section_marks_unsupported_siblings_out_of_scope():
    """When diagnosis names siblings but no single diagnosis_additional_fix_file was set,
    the prompt must say those files are simply out of scope for this call — not fetchable."""
    agent = _make_agent()
    incident = _make_incident(
        diagnosis_additional_fix="Same bug exists in 7 sibling *InterviewUsers.js files.",
        diagnosis_additional_fix_file=None,
        diagnosis_additional_fix_function=None,
    )

    text = await _capture_prompt(agent, incident)

    assert "are NOT fixed automatically and are out of scope for this call" in text
    assert "Do NOT call read_file on any file other than src/target.js" in text


@pytest.mark.asyncio
async def test_additional_fix_section_absent_when_no_additional_fix():
    agent = _make_agent()
    incident = _make_incident(diagnosis_additional_fix=None)

    text = await _capture_prompt(agent, incident)

    assert "DIAGNOSIS NOTE" not in text
    assert "out of scope for this call" not in text


# ---------------------------------------------------------------------------
# _resolve_secondary_targets — every sibling from blast_radius, not just the
# one legacy diagnosis_additional_fix_file
# ---------------------------------------------------------------------------

def test_resolve_secondary_targets_uses_full_blast_radius():
    """A diagnosis naming 7 siblings via blast_radius must yield all 7, not just
    the single diagnosis_additional_fix_file — this was the actual production bug
    (incident 4caba3f3: diagnosis found 8 files, only 1 got a committed fix)."""
    agent = _make_agent()
    incident = _make_incident(
        diagnosis_blast_radius=[
            {"file": "routes/api/inspiringInterviewUsers.js", "function": "handler"},  # primary — excluded
            {"file": "routes/api/shoutoutInterviewUsers.js", "function": "handler"},
            {"file": "routes/api/crInterviewUsers.js", "function": "handler"},
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "handler"},
        ],
        diagnosis_additional_fix_file="routes/api/shoutoutInterviewUsers.js",
        diagnosis_additional_fix_function="handler",
    )

    targets = agent._resolve_secondary_targets(incident, "routes/api/inspiringInterviewUsers.js")

    files = [f for f, _, _ in targets]
    assert "routes/api/inspiringInterviewUsers.js" not in files  # primary excluded
    assert files == [
        "routes/api/shoutoutInterviewUsers.js",
        "routes/api/crInterviewUsers.js",
        "routes/api/boldJourneyInterviewUsers.js",
    ]  # shoutout not duplicated even though it's also diagnosis_additional_fix_file


def test_resolve_secondary_targets_includes_legacy_field_not_in_blast_radius():
    """diagnosis_additional_fix_file must still count when blast_radius omits it —
    don't regress the pre-existing single-secondary-file path."""
    agent = _make_agent()
    incident = _make_incident(
        diagnosis_blast_radius=[],
        diagnosis_additional_fix_file="routes/api/shoutoutInterviewUsers.js",
        diagnosis_additional_fix_function="handler",
    )

    targets = agent._resolve_secondary_targets(incident, "routes/api/inspiringInterviewUsers.js")

    assert targets == [("routes/api/shoutoutInterviewUsers.js", "handler", None)]


def test_resolve_secondary_targets_carries_the_blast_radius_snippet():
    """Real production bug (AllInterviews PR #2552): 3 of 5 secondary files got a
    fabricated new route instead of the real fix, because the secondary pass had
    only a vague function label and free-text prose to go on -- no actual code to
    search for. The blast_radius snippet is the concrete anchor that fixes this;
    it must actually flow through, not get dropped."""
    agent = _make_agent()
    incident = _make_incident(
        diagnosis_blast_radius=[
            {
                "file": "routes/api/inspiringInterviewUsers.js",
                "function": "handler",
                "snippet": "primary — excluded",
            },
            {
                "file": "routes/api/shoutoutInterviewUsers.js",
                "function": "(anonymous route handler)",
                "snippet": "ShoutoutInterviewUser.find({ previewCode: id }).then(users => {",
            },
        ],
    )

    targets = agent._resolve_secondary_targets(incident, "routes/api/inspiringInterviewUsers.js")

    assert targets == [(
        "routes/api/shoutoutInterviewUsers.js",
        "(anonymous route handler)",
        "ShoutoutInterviewUser.find({ previewCode: id }).then(users => {",
    )]


def test_resolve_secondary_targets_caps_at_max():
    agent = _make_agent()
    incident = _make_incident(
        diagnosis_blast_radius=[
            {"file": f"routes/api/brand{i}.js", "function": "handler"} for i in range(20)
        ],
    )

    targets = agent._resolve_secondary_targets(incident, "routes/api/primary.js")

    assert len(targets) == 10  # _MAX_SECONDARY_FIXES


def test_resolve_secondary_targets_empty_when_no_signal():
    agent = _make_agent()
    incident = _make_incident(diagnosis_blast_radius=[], diagnosis_additional_fix_file=None)

    assert agent._resolve_secondary_targets(incident, "routes/api/primary.js") == []


# ---------------------------------------------------------------------------
# _skipped_secondary_files — regression test for a real production crash
# ---------------------------------------------------------------------------

def test_skipped_secondary_files_handles_the_3_tuple_shape():
    """Real production bug: FixGenerationAgent raised 'too many values to
    unpack (expected 2)' on every multi-file incident, because this exact
    computation was an inline `for p, _ in secondary_targets` left over from
    before _resolve_secondary_targets() was extended to 3-tuples. Any incident
    with diagnosis_blast_radius populated hit this on every single run."""
    agent = _make_agent()
    targets = [
        ("routes/api/shoutoutInterviewUsers.js", "handler", "snippet A"),
        ("routes/api/crInterviewUsers.js", None, None),
        ("routes/api/boldJourneyInterviewUsers.js", "handler", "snippet C"),
    ]

    skipped = agent._skipped_secondary_files(
        targets, secondary_files_changed=["routes/api/shoutoutInterviewUsers.js"],
    )

    assert skipped == ["routes/api/crInterviewUsers.js", "routes/api/boldJourneyInterviewUsers.js"]


def test_skipped_secondary_files_empty_when_all_fixed():
    agent = _make_agent()
    targets = [("routes/api/a.js", "fn", None), ("routes/api/b.js", None, "snip")]

    skipped = agent._skipped_secondary_files(
        targets, secondary_files_changed=["routes/api/a.js", "routes/api/b.js"],
    )

    assert skipped == []


# ---------------------------------------------------------------------------
# ROOT CAUSE RULE 6 — reason about validation shape, don't reach for
# isNaN(Number(x)) as a reflexive habit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prompt_warns_against_loose_number_coercion_validation():
    """Real feedback on a shipped fix: FixGenerationAgent used isNaN(Number(id))
    to validate a previewCode param. That accepts '1e5' (-> 100000) and '0x1A'
    (-> 26) as 'valid numbers', neither of which is the plain digit string the
    field actually expects -- a materially weaker guard than what's already
    correct elsewhere in this codebase. The prompt must make the agent reason
    about the actual expected shape rather than hardcoding one 'correct' regex."""
    agent = _make_agent()
    incident = _make_incident(diagnosis="x")

    text = await _capture_prompt(agent, incident)

    assert "isNaN(Number(x))" in text
    assert "1e5" in text and "100000" in text  # scientific notation pitfall
    assert "0x1A" in text  # hex pitfall
    # Must not mandate one specific regex as THE answer -- the point is to
    # teach the reasoning, not hardcode a pattern for the agent to parrot.
    assert "/^\\d+$/" not in text
