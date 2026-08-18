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


def _make_agent(search_code_side_effect, local_repo=None):
    """Build a DiagnosisAgent with stubbed external services.

    local_repo defaults to a mock with ready=False, which makes
    _file_exists_in_repo / _snippet_is_grounded fail open (return True) —
    i.e. "can't verify, assume it's fine" — matching this file's original
    tests, which only exercise function-name grounding via _github.search_code,
    not the local-clone-backed file/snippet checks. Pass an explicit local_repo
    (ready=True, with a real read_file) to test those checks directly.
    """
    agent = DiagnosisAgent.__new__(DiagnosisAgent)
    agent._owner = "owner"
    agent._repo = "repo"
    agent._aws = MagicMock()
    agent._rag = None
    agent._github = MagicMock()
    agent._github.search_code = AsyncMock(side_effect=search_code_side_effect)
    agent._local_repo = local_repo if local_repo is not None else MagicMock(ready=False)
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
    """Reproduces the processAndStoreImage failure: name doesn't exist → nulled.

    Per commit 3599fb0 ("decouple function/file grounding"), a bad function name
    nulls ONLY the function field — not affected_file. Express anonymous route
    handlers have no searchable symbol, and the file path alone is still useful
    to the fix agent, so it's kept rather than thrown away. Confidence is only
    capped when affected_file is ALSO null.
    """
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
    assert out.affected_file == "routes/services/image-service.js"  # kept, not nulled
    assert out.confidence == 0.91  # not capped — the file is still verified
    assert out.escalate is False
    assert any("GROUNDING NOTE" in e and "processAndStoreImage" in e for e in out.evidence)


@pytest.mark.asyncio
async def test_grounding_nulls_fabricated_secondary_function():
    """A fabricated additional_fix_function nulls only itself, not additional_fix_file
    (same decoupled-grounding rule as the primary field — see commit 3599fb0)."""
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
    assert out.additional_fix_function is None  # secondary function nulled
    assert out.additional_fix_file == "b.js"  # secondary file kept — affected_file also verified
    assert out.confidence == 0.85  # not capped — affected_file is verified
    assert out.escalate is False


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


# ---------------------------------------------------------------------------
# Pre-fix-reasoning fields: blast_radius + contract_change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parser_extracts_blast_radius_and_contract_change():
    """Schema-shaped JSON should populate the new fields."""
    from app.agents.diagnosis import _parse_diagnosis_result

    answer = """```json
    {
      "root_cause": "x",
      "confidence": 0.85,
      "fix_approach": "y",
      "affected_function": "processOrder",
      "affected_file": "src/orders.js",
      "reproduction_confirmed": true,
      "blast_radius": [
        {"file": "src/api.js", "function": "submitOrder", "snippet": "await processOrder(payload)"},
        {"file": "src/cron/retry.js", "function": "retryFailed", "snippet": "processOrder(...)"}
      ],
      "contract_change": "signature",
      "contract_change_detail": "added retries param"
    }
    ```"""

    result = _parse_diagnosis_result(answer)
    assert result.affected_function == "processOrder"
    assert len(result.blast_radius) == 2
    assert result.blast_radius[0]["file"] == "src/api.js"
    assert result.blast_radius[0]["function"] == "submitOrder"
    assert result.contract_change == "signature"
    assert result.contract_change_detail == "added retries param"


@pytest.mark.asyncio
async def test_parser_drops_invalid_blast_radius_entries():
    """Entries missing `file` should be dropped silently rather than crashing."""
    from app.agents.diagnosis import _parse_diagnosis_result

    answer = """{
      "root_cause": "x",
      "confidence": 0.85,
      "blast_radius": [
        {"file": "src/a.js", "function": "callerA"},
        {"function": "noFile"},
        "not a dict"
      ]
    }"""

    result = _parse_diagnosis_result(answer)
    assert len(result.blast_radius) == 1
    assert result.blast_radius[0]["file"] == "src/a.js"


@pytest.mark.asyncio
async def test_parser_normalises_unknown_contract_change():
    """Unknown contract_change values must clamp to 'none' rather than leaking through."""
    from app.agents.diagnosis import _parse_diagnosis_result

    answer = """{
      "root_cause": "x",
      "confidence": 0.85,
      "contract_change": "???"
    }"""
    result = _parse_diagnosis_result(answer)
    assert result.contract_change == "none"


@pytest.mark.asyncio
async def test_grounding_warns_when_blast_radius_empty_for_helper():
    """Empty blast_radius on a non-entry-point function adds an evidence note."""
    async def found(owner, repo, query):
        return [{"path": "src/x.js", "fragment": "function processOrder() {}"}]

    agent = _make_agent(found)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="processOrder",
        affected_file="src/orders.js",
        blast_radius=[],   # no callers reported
    )

    out = await agent._enforce_grounding(result)

    assert out.confidence == 0.9   # not capped — this is observability, not correctness
    assert any("BLAST RADIUS WARNING" in e for e in out.evidence)


@pytest.mark.asyncio
async def test_grounding_skips_blast_radius_warning_for_entry_points():
    """Route handlers / cron jobs naturally have no callers — no warning."""
    async def found(owner, repo, query):
        return [{"path": "src/routes/api.js", "fragment": "function paymentHandler() {}"}]

    agent = _make_agent(found)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="paymentHandler",
        affected_file="src/routes/api.js",
        blast_radius=[],
    )

    out = await agent._enforce_grounding(result)
    assert all("BLAST RADIUS WARNING" not in e for e in out.evidence)


@pytest.mark.asyncio
async def test_grounding_skips_blast_radius_warning_when_evidence_explains():
    """If the diagnosis evidence already says 'no callers — entry point', no warning."""
    async def found(owner, repo, query):
        return [{"path": "src/x.js", "fragment": "function processOrder() {}"}]

    agent = _make_agent(found)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="processOrder",
        affected_file="src/x.js",
        blast_radius=[],
        evidence=["affected_function is a route handler — no callers"],
    )

    out = await agent._enforce_grounding(result)
    assert all("BLAST RADIUS WARNING" not in e for e in out.evidence)


@pytest.mark.asyncio
async def test_grounding_no_blast_radius_warning_when_callers_present():
    async def found(owner, repo, query):
        return [{"path": "src/x.js", "fragment": "processOrder()"}]

    agent = _make_agent(found)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="processOrder",
        affected_file="src/x.js",
        blast_radius=[
            {"file": "src/api.js", "function": "submit", "snippet": "processOrder()"},
        ],
    )

    out = await agent._enforce_grounding(result)
    assert all("BLAST RADIUS WARNING" not in e for e in out.evidence)


# ---------------------------------------------------------------------------
# additional_fix_file content-currency grounding (PR #178)
#
# Real production bug: the diagnosis claimed "Apply the identical guard to
# shoutoutInterviewUsers.js and smallBusinessOfTheDayInterviewUsers.js — both
# confirmed by grep to still have the unguarded pattern," but both files had
# already been fixed by earlier, unrelated incidents. additional_fix_file only
# checked file EXISTENCE (always true — the file is real), never whether the
# claimed vulnerability was still CURRENTLY there. blast_radius entries already
# had this protection via _snippet_is_grounded; additional_fix_file did not.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_additional_fix_nulled_when_snippet_not_grounded():
    """A claimed-still-vulnerable file that no longer contains the snippet is nulled."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = (
        "router.get('/getPreviewUser/:id', (req, res) => {\n"
        "  const { id } = req.params;\n"
        "  if (!/^\\d+$/.test(id)) { return res.status(400).json({message: 'Invalid'}); }\n"
        "  Model.find({ previewCode: Number(id) }).then(users => res.json(users));\n"
        "});"
    )
    agent = _make_agent(found, local_repo=local_repo)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix="apply identical guard",
        additional_fix_file="routes/api/shoutoutInterviewUsers.js",
        additional_fix_snippet=(
            "router.get('/getPreviewUser/:id', (req, res) => {\n"
            "  const { id } = req.params;\n"
            "  Model.find({ previewCode: id }).then(users => res.json(users));\n"
            "});"
        ),
    )

    out = await agent._enforce_grounding(result)

    assert out.additional_fix_file is None
    assert out.additional_fix_snippet is None
    assert out.additional_fix_function is None


@pytest.mark.asyncio
async def test_additional_fix_survives_when_snippet_grounded():
    """A claimed-still-vulnerable file that genuinely still has the pattern survives."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    vulnerable_snippet = (
        "router.get('/getPreviewUser/:id', (req, res) => {\n"
        "  const { id } = req.params;\n"
        "  Model.find({ previewCode: id }).then(users => res.json(users));\n"
        "});"
    )
    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = vulnerable_snippet
    agent = _make_agent(found, local_repo=local_repo)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix="apply identical guard",
        additional_fix_file="routes/api/boldJourneyInterviewUsers.js",
        additional_fix_snippet=vulnerable_snippet,
    )

    out = await agent._enforce_grounding(result)

    assert out.additional_fix_file == "routes/api/boldJourneyInterviewUsers.js"
    assert out.additional_fix_snippet == vulnerable_snippet


@pytest.mark.asyncio
async def test_additional_fix_survives_when_cannot_verify_at_all():
    """When _local_repo isn't ready (can't verify anything), fail open — same
    policy as every other check in this file. This is the ONLY case a missing
    snippet doesn't cost the claim its file."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    agent = _make_agent(found)  # default local_repo: ready=False, fails open
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix="add strictQuery to server.js too",
        additional_fix_file="server.js",
    )

    out = await agent._enforce_grounding(result)

    assert out.additional_fix_file == "server.js"


@pytest.mark.asyncio
async def test_additional_fix_nulled_when_snippet_omitted_but_verifiable():
    """Real production bug, recurred AFTER the snippet-grounding check above already
    existed: the model just left additional_fix_snippet out entirely, which skipped
    the check completely and let an unverified "confirmed still-vulnerable" claim
    through. A missing snippet is no more trustworthy than a wrong one when we CAN
    verify — both must be discarded, not just the wrong one."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = "router.get('/getPreviewUser/:id', ...) { /* already has guard */ }"
    agent = _make_agent(found, local_repo=local_repo)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix="apply identical guard to shoutoutInterviewUsers.js — confirmed still vulnerable",
        additional_fix_file="routes/api/shoutoutInterviewUsers.js",
        # no additional_fix_snippet supplied
    )

    out = await agent._enforce_grounding(result)

    assert out.additional_fix_file is None
    assert out.additional_fix_function is None


@pytest.mark.asyncio
async def test_additional_fix_prose_flagged_when_file_field_null_but_prose_names_files():
    """Real production bug: additional_fix_file correctly came back null (no wrong
    commit followed), but additional_fix prose still asserted specific files were
    'confirmed still-vulnerable' with nothing backing it — displayed verbatim on the
    incident dashboard as if it were checked. Prose making an unverifiable per-file
    claim must be flagged, not presented as fact."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    local_repo = MagicMock(ready=True)
    agent = _make_agent(found, local_repo=local_repo)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix=(
            "Apply the identical isNaN guard to shoutoutInterviewUsers.js and "
            "smallBusinessOfTheDayInterviewUsers.js — both confirmed still-vulnerable."
        ),
        additional_fix_file=None,
    )

    out = await agent._enforce_grounding(result)

    assert any("ADDITIONAL_FIX UNVERIFIED" in e for e in out.evidence)


@pytest.mark.asyncio
async def test_additional_fix_prose_not_flagged_without_file_mentions():
    """Prose that doesn't name a specific file (e.g. a pure conceptual description)
    isn't penalized — the flag is specifically for unverifiable per-file claims."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    local_repo = MagicMock(ready=True)
    agent = _make_agent(found, local_repo=local_repo)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix="Also validate the same field server-side on the client form.",
        additional_fix_file=None,
    )

    out = await agent._enforce_grounding(result)

    assert all("ADDITIONAL_FIX UNVERIFIED" not in e for e in out.evidence)


# ---------------------------------------------------------------------------
# additional_fix_targets — multi-file counterpart to additional_fix_file (PR #180)
#
# Real production bug: a diagnosis correctly identified 3 sibling files needing
# the identical per-brand-duplication fix in its prose, but additional_fix_file
# can only ever carry ONE — FixGenerationAgent structurally never had a path to
# attempt more than one secondary fix, even when the diagnosis got every file
# right. additional_fix_targets is the fix: a proper list, each entry grounded
# the same (mandatory-snippet) way as additional_fix_file.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_parser_extracts_additional_fix_targets():
    """Schema-shaped JSON should populate additional_fix_targets."""
    from app.agents.diagnosis import _parse_diagnosis_result

    answer = """{
      "root_cause": "x",
      "confidence": 0.85,
      "additional_fix_targets": [
        {"file": "routes/api/boldJourneyInterviewUsers.js", "function": null, "snippet": "Model.find({ previewCode: id })"},
        {"file": "routes/api/inspiringInterviewUsers.js", "function": null, "snippet": "Model.find({ previewCode: id })"}
      ]
    }"""

    result = _parse_diagnosis_result(answer)
    assert len(result.additional_fix_targets) == 2
    assert result.additional_fix_targets[0]["file"] == "routes/api/boldJourneyInterviewUsers.js"


@pytest.mark.asyncio
async def test_additional_fix_targets_survive_when_grounded():
    """Multiple genuinely-vulnerable sibling files, each with a real snippet, all
    survive — this is the whole point of the field."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    vulnerable_snippet = "Model.find({ previewCode: id }).then(users => res.json(users));"
    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = f"router.get('/getPreviewUser/:id', (req, res) => {{ {vulnerable_snippet} }});"
    agent = _make_agent(found, local_repo=local_repo)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix_targets=[
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "", "snippet": vulnerable_snippet},
            {"file": "routes/api/inspiringInterviewUsers.js", "function": "", "snippet": vulnerable_snippet},
        ],
    )

    out = await agent._enforce_grounding(result)

    assert len(out.additional_fix_targets) == 2


@pytest.mark.asyncio
async def test_additional_fix_targets_drops_entries_without_grounded_snippet():
    """Real production bug: 1 of 3 named sibling files was already fixed (wrong
    claim) — each entry must be checked independently, not accepted as a batch."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    already_fixed_content = "if (!/^\\d+$/.test(id)) { return res.status(400).json({}); } Model.find({ previewCode: Number(id) })"
    local_repo = MagicMock(ready=True)
    # shoutout is already fixed (real content doesn't match the claimed vulnerable snippet);
    # boldJourney genuinely still has it.
    local_repo.read_file.side_effect = lambda path: (
        already_fixed_content if "shoutout" in path else "Model.find({ previewCode: id })"
    )
    agent = _make_agent(found, local_repo=local_repo)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix_targets=[
            {"file": "routes/api/shoutoutInterviewUsers.js", "function": "", "snippet": "Model.find({ previewCode: id })"},
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "", "snippet": "Model.find({ previewCode: id })"},
        ],
    )

    out = await agent._enforce_grounding(result)

    surviving_files = [e["file"] for e in out.additional_fix_targets]
    assert surviving_files == ["routes/api/boldJourneyInterviewUsers.js"]


@pytest.mark.asyncio
async def test_additional_fix_targets_drops_entries_with_no_snippet_at_all():
    """Omitting the snippet is not a way around verification — same policy as
    additional_fix_file."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = "Model.find({ previewCode: id })"
    agent = _make_agent(found, local_repo=local_repo)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix_targets=[
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "", "snippet": ""},
        ],
    )

    out = await agent._enforce_grounding(result)

    assert out.additional_fix_targets == []


@pytest.mark.asyncio
async def test_additional_fix_targets_survive_when_cannot_verify_at_all():
    """Fail-open when _local_repo isn't ready — same policy as every other check."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    agent = _make_agent(found)  # default: ready=False
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix_targets=[
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "", "snippet": ""},
        ],
    )

    out = await agent._enforce_grounding(result)

    assert len(out.additional_fix_targets) == 1


@pytest.mark.asyncio
async def test_additional_fix_prose_not_flagged_when_targets_grounded():
    """additional_fix_file is None, but additional_fix_targets is genuinely
    grounded — the prose-unverified flag must not fire in this case."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    vulnerable_snippet = "Model.find({ previewCode: id })"
    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = vulnerable_snippet
    agent = _make_agent(found, local_repo=local_repo)
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        affected_function="primaryFn",
        affected_file="a.js",
        additional_fix="Apply the identical guard to boldJourneyInterviewUsers.js.",
        additional_fix_file=None,
        additional_fix_targets=[
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "", "snippet": vulnerable_snippet},
        ],
    )

    out = await agent._enforce_grounding(result)

    assert all("ADDITIONAL_FIX UNVERIFIED" not in e for e in out.evidence)


@pytest.mark.asyncio
async def test_entry_point_helper_name_hints():
    """Spot-check the entry-point name detector."""
    from app.agents.diagnosis import _looks_like_entry_point

    # Names that suggest entry points:
    assert _looks_like_entry_point("paymentHandler", [])
    assert _looks_like_entry_point("getUserRoute", [])
    assert _looks_like_entry_point("nightlyCron", [])
    assert _looks_like_entry_point("queueWorker", [])

    # Names that don't:
    assert not _looks_like_entry_point("processOrder", [])
    assert not _looks_like_entry_point("validateInput", [])

    # Evidence override:
    assert _looks_like_entry_point("processOrder", ["this is the express handler — no callers"])
