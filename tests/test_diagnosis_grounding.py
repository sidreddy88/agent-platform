"""
Regression tests for DiagnosisAgent's code-grounding guard.

The guard prevents fabricated function names (a name the LLM invented that does not
actually exist on the target repo's default branch) from leaking into downstream
agents. Reproduces the failure mode where the diagnosis agent named
`processAndStoreImage` — a symbol that didn't exist anywhere in the repo — and
the Fix Generation agent then targeted a non-existent function.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.diagnosis import CONFIDENCE_THRESHOLD, DiagnosisAgent, DiagnosisResult


def _make_agent(search_code_side_effect):
    """Build a DiagnosisAgent with stubbed external services."""
    agent = DiagnosisAgent.__new__(DiagnosisAgent)
    agent._owner = "owner"
    agent._repo = "repo"
    agent._aws = MagicMock()
    agent._rag = None
    agent._github = MagicMock()
    agent._github.search_code = AsyncMock(side_effect=search_code_side_effect)
    return agent


@pytest.mark.asyncio
async def test_grounding_passes_when_function_exists():
    async def found(owner, repo, query):
        return [{"path": "routes/services/image.js", "fragment": "function realFn() {}"}]

    agent = _make_agent(found)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="realFn",
        affected_file="routes/services/image.js",
    )

    out = await agent._enforce_grounding(result)

    assert out.affected_function == "realFn"
    assert out.affected_file == "routes/services/image.js"
    assert out.confidence == 0.9
    assert out.escalate is False
    assert all("GROUNDING FAILURE" not in e for e in out.evidence)


@pytest.mark.asyncio
async def test_grounding_nulls_fabricated_function():
    """Reproduces the processAndStoreImage failure: name doesn't exist → must be nulled."""
    async def not_found(owner, repo, query):
        return []

    agent = _make_agent(not_found)
    result = DiagnosisResult(
        root_cause="HEIF upload, sharp throws on unsupported codec",
        confidence=0.91,
        evidence=["log: bad seek to 1668282", "log: Unsupported codec (4.3000)"],
        affected_function="processAndStoreImage",
        affected_file="routes/services/image-service.js",
    )

    out = await agent._enforce_grounding(result)

    assert out.affected_function is None
    assert out.affected_file is None
    assert out.confidence <= 0.65
    assert out.escalate is True  # 0.65 < CONFIDENCE_THRESHOLD (0.70)
    assert any("GROUNDING FAILURE" in e and "processAndStoreImage" in e for e in out.evidence)


@pytest.mark.asyncio
async def test_grounding_nulls_fabricated_secondary_function():
    async def selective(owner, repo, query):
        # Primary function exists; secondary does not.
        if "primaryFn" in query:
            return [{"path": "a.js", "fragment": "primaryFn()"}]
        return []

    agent = _make_agent(selective)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.85,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix="cleanup",
        additional_fix_function="madeUpHelper",
        additional_fix_file="b.js",
    )

    out = await agent._enforce_grounding(result)

    assert out.affected_function == "primaryFn"  # primary survives
    assert out.affected_file == "a.js"
    assert out.additional_fix_function is None  # secondary nulled
    assert out.additional_fix_file is None
    assert out.confidence <= 0.65
    assert out.escalate is True


@pytest.mark.asyncio
async def test_grounding_skipped_when_no_function_named():
    """Low-confidence diagnosis with no function named should not be re-capped."""
    async def never_called(owner, repo, query):  # pragma: no cover
        raise AssertionError("search_code should not be called when no function is named")

    agent = _make_agent(never_called)
    result = DiagnosisResult(
        root_cause="unclear",
        confidence=0.55,
        affected_function=None,
        affected_file=None,
        escalate=True,
    )

    out = await agent._enforce_grounding(result)

    assert out.confidence == 0.55
    assert out.escalate is True
    assert all("GROUNDING FAILURE" not in e for e in out.evidence)


@pytest.mark.asyncio
async def test_confidence_threshold_constant():
    """Sanity: changing CONFIDENCE_THRESHOLD must keep the guard's escalation aligned."""
    assert CONFIDENCE_THRESHOLD == 0.70


@pytest.mark.asyncio
async def test_prose_scan_catches_fabricated_name_in_root_cause():
    """The callVisionAPI failure mode: structured fields look clean, prose lies."""
    async def not_found(owner, repo, query):
        return []

    agent = _make_agent(not_found)
    result = DiagnosisResult(
        root_cause=(
            "callVisionAPI in tasks/imageADATask.js posts to VISION_API_ENDPOINT "
            "with the hardcoded model identifier 'gpt-4-vision'."
        ),
        confidence=0.65,
        affected_function=None,           # structured slot already null
        affected_file=None,
        additional_fix=(
            "In processAndStoreImage, skip the analyzeImageWithLLM fire-and-forget call."
        ),
    )

    out = await agent._enforce_grounding(result)

    # Prose scan should fire — confidence capped tighter than the structured cap (0.65).
    assert out.confidence <= 0.55
    assert out.escalate is True
    note = next((e for e in out.evidence if "PROSE GROUNDING" in e), None)
    assert note is not None
    # All three fabricated names should be flagged.
    assert "callVisionAPI" in note
    assert "processAndStoreImage" in note
    assert "analyzeImageWithLLM" in note


@pytest.mark.asyncio
async def test_prose_scan_skips_verified_structured_names():
    """A name already verified in affected_function shouldn't be re-checked in prose."""
    calls = []

    async def found_once(owner, repo, query):
        calls.append(query)
        return [{"path": "a.js", "fragment": "realFn()"}]

    agent = _make_agent(found_once)
    result = DiagnosisResult(
        root_cause="realFn fails when buffer is null",
        fix_approach="guard realFn against null input",
        confidence=0.9,
        affected_function="realFn",
        affected_file="a.js",
    )

    out = await agent._enforce_grounding(result)

    # Only the structured-field check should run; prose mention dedupes.
    assert len(calls) <= 2  # at most 2 queries for the single structured check
    assert out.confidence == 0.9
    assert out.escalate is False


@pytest.mark.asyncio
async def test_prose_scan_skips_builtins():
    """toString, forEach, etc. should never be verified — they're stdlib noise."""
    async def fail_if_called(owner, repo, query):  # pragma: no cover
        raise AssertionError(f"should not query for builtin: {query}")

    agent = _make_agent(fail_if_called)
    result = DiagnosisResult(
        root_cause="caller invokes toString() and forEach() on undefined input",
        confidence=0.85,
        affected_function=None,
        affected_file=None,
    )

    out = await agent._enforce_grounding(result)

    assert out.confidence == 0.85
    assert out.escalate is False


@pytest.mark.asyncio
async def test_prose_scan_extraction_patterns():
    """Direct test of the extractor to lock in regex behavior."""
    from app.agents.diagnosis import _extract_prose_symbols

    text = (
        "We saw `callVisionAPI` fail. Then processAndStoreImage(buf) threw. "
        "Also analyzeImageWithLLM() and `helperFn`. Builtin: toString() — skip. "
        "lower (no caps) — skip. ABC (all caps) — skip. abc (no caps) — skip."
    )
    names = _extract_prose_symbols(text)

    assert "callVisionAPI" in names
    assert "processAndStoreImage" in names
    assert "analyzeImageWithLLM" in names
    assert "helperFn" in names
    assert "toString" not in names  # builtin filtered
    assert "lower" not in names      # no capital
    assert "ABC" not in names        # doesn't start lowercase
