"""
Regression tests for DiagnosisAgent's code-grounding guard.

Two layers, tested separately:
  - _validate_diagnosis_submission: the submit_diagnosis tool's inline gate.
    Runs BEFORE a diagnosis is accepted -- a failure here becomes a same-turn
    tool rejection (a list of problem strings), not a silent null-and-cap.
    This is where fabricated function names, file paths, file<->function
    pairings, and snippets (primary + additional_fix_file + every
    additional_fix_targets/blast_radius entry) get caught, plus the
    prose-names-a-file-with-no-structured-backing check.
  - _enforce_grounding: the smaller, complementary check that still runs
    AFTER a submission is accepted -- prose symbol-scan (fabricated names
    mentioned only in reasoning, never assigned to any field the submission
    gate validates) and blast_radius coverage (a completeness nudge, not a
    fabrication check).

Reproduces several real production fabrications, preserved from this file's
history: `processAndStoreImage` (a symbol that didn't exist anywhere in the
repo), sibling files claimed "still vulnerable" that had already been fixed
by earlier incidents, and the worst one found this session -- a diagnosis
that named a real file (`models/MasterInspiring.js`) and quoted a root-cause
snippet that existed nowhere in the actual repo.
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


# ---------------------------------------------------------------------------
# _validate_diagnosis_submission — the submit_diagnosis inline gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submission_passes_when_function_exists():
    async def found(owner, repo, query):
        return [{"path": "routes/services/image.js", "fragment": "function realFn() {}"}]

    agent = _make_agent(found)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "realFn", "affected_file": "routes/services/image.js",
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert problems == []


@pytest.mark.asyncio
async def test_submission_rejects_fabricated_function():
    """Reproduces the processAndStoreImage failure: name doesn't exist → rejected."""
    async def not_found(owner, repo, query):
        return []

    agent = _make_agent(not_found)
    data = {
        "root_cause": "HEIF upload, sharp throws on unsupported codec",
        "confidence": 0.91,
        "affected_function": "processAndStoreImage",
        "affected_file": "routes/services/image-service.js",
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("processAndStoreImage" in p for p in problems)


@pytest.mark.asyncio
async def test_submission_rejects_fabricated_secondary_function():
    async def selective(owner, repo, query):
        if "primaryFn" in query:
            return [{"path": "a.js", "fragment": "primaryFn()"}]
        return []

    agent = _make_agent(selective)
    data = {
        "root_cause": "x", "confidence": 0.85,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "additional_fix": "cleanup",
        "additional_fix_function": "madeUpHelper", "additional_fix_file": "b.js",
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("madeUpHelper" in p for p in problems)


@pytest.mark.asyncio
async def test_submission_passes_when_no_function_named():
    """Low-confidence diagnosis with no function named has nothing to check here."""
    async def never_called(owner, repo, query):  # pragma: no cover
        raise AssertionError("search_code should not be called when no function is named")

    agent = _make_agent(never_called)
    data = {"root_cause": "unclear", "confidence": 0.55}

    problems = await agent._validate_diagnosis_submission(data)

    assert problems == []


@pytest.mark.asyncio
async def test_confidence_threshold_constant():
    """Sanity: changing CONFIDENCE_THRESHOLD must keep the guard's escalation aligned."""
    assert CONFIDENCE_THRESHOLD == 0.70


# ---------------------------------------------------------------------------
# additional_fix_file content-currency grounding
#
# Real production bug: the diagnosis claimed "Apply the identical guard to
# shoutoutInterviewUsers.js and smallBusinessOfTheDayInterviewUsers.js — both
# confirmed by grep to still have the unguarded pattern," but both files had
# already been fixed by earlier, unrelated incidents.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submission_rejects_additional_fix_when_snippet_not_grounded():
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
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "additional_fix": "apply identical guard",
        "additional_fix_file": "routes/api/shoutoutInterviewUsers.js",
        "additional_fix_snippet": (
            "router.get('/getPreviewUser/:id', (req, res) => {\n"
            "  const { id } = req.params;\n"
            "  Model.find({ previewCode: id }).then(users => res.json(users));\n"
            "});"
        ),
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("additional_fix_file" in p for p in problems)


@pytest.mark.asyncio
async def test_submission_passes_when_additional_fix_snippet_grounded():
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    vulnerable_snippet = (
        "router.get('/getPreviewUser/:id', (req, res) => {\n"
        "  const { id } = req.params;\n"
        "  Model.find({ previewCode: id }).then(users => res.json(users));\n"
        "});"
    )
    local_repo = MagicMock(ready=True)
    local_repo.read_file.side_effect = lambda p: (
        "function primaryFn() {}" if p == "a.js" else vulnerable_snippet
    )
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "root_cause_snippet": "function primaryFn() {}",
        "additional_fix": "apply identical guard",
        "additional_fix_file": "routes/api/boldJourneyInterviewUsers.js",
        "additional_fix_snippet": vulnerable_snippet,
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert problems == []


@pytest.mark.asyncio
async def test_submission_passes_when_cannot_verify_at_all():
    """Fail-open when _local_repo isn't ready — same policy as every check."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    agent = _make_agent(found)  # default local_repo: ready=False, fails open
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "additional_fix": "add strictQuery to server.js too",
        "additional_fix_file": "server.js",
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert problems == []


@pytest.mark.asyncio
async def test_submission_rejects_additional_fix_when_snippet_omitted_but_verifiable():
    """Real production bug: the model left additional_fix_snippet out entirely,
    which skipped verification completely and let an unverified "confirmed
    still-vulnerable" claim through. A missing snippet is no more trustworthy
    than a wrong one when we CAN verify."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = "router.get('/getPreviewUser/:id', ...) { /* already has guard */ }"
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "additional_fix": "apply identical guard to shoutoutInterviewUsers.js — confirmed still vulnerable",
        "additional_fix_file": "routes/api/shoutoutInterviewUsers.js",
        # no additional_fix_snippet supplied
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("additional_fix_file" in p for p in problems)


@pytest.mark.asyncio
async def test_submission_rejects_prose_naming_file_with_no_structured_backing():
    """Real production bug: additional_fix_file correctly came back null (no wrong
    commit followed), but additional_fix prose still asserted specific files were
    'confirmed still-vulnerable' with nothing backing it — displayed verbatim on the
    incident dashboard as if it were checked."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    local_repo = MagicMock(ready=True)
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "additional_fix": (
            "Apply the identical isNaN guard to shoutoutInterviewUsers.js and "
            "smallBusinessOfTheDayInterviewUsers.js — both confirmed still-vulnerable."
        ),
        "additional_fix_file": None,
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("shoutoutInterviewUsers.js" in p for p in problems)
    assert any("smallBusinessOfTheDayInterviewUsers.js" in p for p in problems)


@pytest.mark.asyncio
async def test_submission_passes_prose_without_file_mentions():
    """Prose that doesn't name a specific file (e.g. a pure conceptual description)
    isn't penalized — the check is specifically for unverifiable per-file claims."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = "function primaryFn() {}"
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "root_cause_snippet": "function primaryFn() {}",
        "additional_fix": "Also validate the same field server-side on the client form.",
        "additional_fix_file": None,
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert problems == []


# ---------------------------------------------------------------------------
# additional_fix_targets — multi-file counterpart to additional_fix_file
#
# Real production bug: a diagnosis correctly identified 3 sibling files needing
# the identical per-brand-duplication fix in its prose, but additional_fix_file
# can only ever carry ONE.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submission_passes_when_targets_grounded():
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    vulnerable_snippet = "Model.find({ previewCode: id }).then(users => res.json(users));"
    sibling_content = f"router.get('/getPreviewUser/:id', (req, res) => {{ {vulnerable_snippet} }});"
    local_repo = MagicMock(ready=True)
    local_repo.read_file.side_effect = lambda p: (
        "function primaryFn() {}" if p == "a.js" else sibling_content
    )
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "root_cause_snippet": "function primaryFn() {}",
        "additional_fix_targets": [
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "", "snippet": vulnerable_snippet},
            {"file": "routes/api/inspiringInterviewUsers.js", "function": "", "snippet": vulnerable_snippet},
        ],
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert problems == []


@pytest.mark.asyncio
async def test_submission_rejects_targets_without_grounded_snippet():
    """Real production bug: 1 of 3 named sibling files was already fixed (wrong
    claim) — each entry must be checked independently, not accepted as a batch."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    already_fixed_content = "if (!/^\\d+$/.test(id)) { return res.status(400).json({}); } Model.find({ previewCode: Number(id) })"
    local_repo = MagicMock(ready=True)
    local_repo.read_file.side_effect = lambda path: (
        already_fixed_content if "shoutout" in path else "Model.find({ previewCode: id })"
    )
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "additional_fix_targets": [
            {"file": "routes/api/shoutoutInterviewUsers.js", "function": "", "snippet": "Model.find({ previewCode: id })"},
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "", "snippet": "Model.find({ previewCode: id })"},
        ],
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("shoutoutInterviewUsers.js" in p and "[0]" in p for p in problems)
    assert not any("boldJourneyInterviewUsers.js" in p for p in problems)


@pytest.mark.asyncio
async def test_submission_rejects_targets_with_no_snippet_at_all():
    """Omitting the snippet is not a way around verification — same policy as
    additional_fix_file."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = "Model.find({ previewCode: id })"
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "additional_fix_targets": [
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "", "snippet": ""},
        ],
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("boldJourneyInterviewUsers.js" in p for p in problems)


@pytest.mark.asyncio
async def test_submission_passes_targets_when_cannot_verify_at_all():
    """Fail-open when _local_repo isn't ready — same policy as every other check."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    agent = _make_agent(found)  # default: ready=False
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "additional_fix_targets": [
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "", "snippet": ""},
        ],
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert problems == []


@pytest.mark.asyncio
async def test_submission_passes_prose_when_targets_grounded():
    """additional_fix_file is None, but additional_fix_targets is genuinely
    grounded — the prose-mention check must not fire in this case."""
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    vulnerable_snippet = "Model.find({ previewCode: id })"
    local_repo = MagicMock(ready=True)
    local_repo.read_file.side_effect = lambda p: (
        "function primaryFn() {}" if p == "a.js" else vulnerable_snippet
    )
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "primaryFn", "affected_file": "a.js",
        "root_cause_snippet": "function primaryFn() {}",
        "additional_fix": "Apply the identical guard to boldJourneyInterviewUsers.js.",
        "additional_fix_file": None,
        "additional_fix_targets": [
            {"file": "routes/api/boldJourneyInterviewUsers.js", "function": "", "snippet": vulnerable_snippet},
        ],
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert problems == []


# ---------------------------------------------------------------------------
# affected_file / root_cause_snippet grounding — the PRIMARY-target counterpart
# to additional_fix_file's mandatory-snippet policy.
#
# Real production bug, the worst fabrication found this session: a diagnosis
# named affected_file="models/MasterInspiring.js" (a real file, module-level,
# no function claimed) and quoted a root_cause code snippet
# ("errors: { prank: {...}, contentFlags: {...} }") that existed NOWHERE in
# the real repo -- not in that file, not in any file.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_submission_passes_when_root_cause_snippet_grounded():
    async def found(owner, repo, query):
        return []

    real_snippet = "errors: { type: Array },"
    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = (
        "const schema = new mongoose.Schema({\n  fields: { type: Object },\n"
        f"  {real_snippet}\n  result: {{ type: String }},\n}});"
    )
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "PrankCheckerLog.js defines a reserved `errors` schema pathname",
        "confidence": 0.85,
        "affected_file": "models/PrankCheckerLog.js",
        "root_cause_snippet": real_snippet,
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert problems == []


@pytest.mark.asyncio
async def test_submission_rejects_fabricated_root_cause_snippet():
    """The actual real-world bug: a snippet that matches nothing in the real file."""
    async def found(owner, repo, query):
        return []

    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = (
        "const MasterInspiringSchema = new schema({\n"
        "  email: { type: String, required: true, unique: true },\n"
        "}, { timestamps: true });"
    )
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "All four Mongoose model files define an errors field...",
        "confidence": 0.55,
        "affected_file": "models/MasterInspiring.js",
        "root_cause_snippet": "errors: { prank: { type: Boolean, default: false }, contentFlags: { type: Array, default: [] } }",
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("root_cause_snippet" in p for p in problems)


@pytest.mark.asyncio
async def test_submission_rejects_root_cause_snippet_omitted_but_verifiable():
    """Omitting the snippet is not a way around verification."""
    async def found(owner, repo, query):
        return []

    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = "some real file content"
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_file": "models/PrankCheckerLog.js",
        # no root_cause_snippet
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("root_cause_snippet" in p for p in problems)


@pytest.mark.asyncio
async def test_submission_passes_when_cannot_verify_affected_file_at_all():
    """Fail-open when _local_repo isn't ready — same policy as every other check."""
    async def found(owner, repo, query):
        return []

    agent = _make_agent(found)  # default: ready=False
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_file": "models/PrankCheckerLog.js",
        # no root_cause_snippet — still passes since we can't verify at all
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert problems == []


@pytest.mark.asyncio
async def test_submission_rejects_function_not_paired_with_file():
    """A real fabrication slipped through exactly this gap in production: the LLM
    claimed a real symbol (found via search_code — just in a different file) was
    defined in a real file — just without that symbol. Both individual checks
    passed; the pairing was never verified."""
    async def found(owner, repo, query):
        return [{"path": "some/other/file.js", "fragment": "REFERRAL_MODEL_MAP"}]

    local_repo = MagicMock(ready=True)
    local_repo.read_file.return_value = "module.exports = { run() { /* nothing relevant here */ } };"
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_function": "REFERRAL_MODEL_MAP",
        "affected_file": "create-post-fargate.js",
        "root_cause_snippet": "some snippet",
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("does not appear inside" in p for p in problems)


@pytest.mark.asyncio
async def test_submission_rejects_nonexistent_affected_file():
    async def found(owner, repo, query):
        return [{"path": "a.js", "fragment": "primaryFn()"}]

    local_repo = MagicMock(ready=True)
    local_repo.file_exists.return_value = False
    agent = _make_agent(found, local_repo=local_repo)
    data = {
        "root_cause": "x", "confidence": 0.9,
        "affected_file": "does/not/exist.js",
    }

    problems = await agent._validate_diagnosis_submission(data)

    assert any("does not exist" in p for p in problems)


# ---------------------------------------------------------------------------
# _enforce_grounding — complementary checks that run AFTER acceptance
# (prose symbol-scan + blast_radius coverage). Unaffected by the
# submit_diagnosis migration; kept as regression coverage.
# ---------------------------------------------------------------------------

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
        affected_function=None,
        affected_file=None,
        additional_fix=(
            "In processAndStoreImage, skip the analyzeImageWithLLM fire-and-forget call."
        ),
    )

    out = await agent._enforce_grounding(result)

    assert out.confidence <= 0.55
    assert out.escalate is True
    note = next((e for e in out.evidence if "PROSE GROUNDING" in e), None)
    assert note is not None
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

    assert len(calls) == 0  # nothing left in prose that isn't already in already_seen
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
    assert "toString" not in names
    assert "lower" not in names
    assert "ABC" not in names


@pytest.mark.asyncio
async def test_grounding_warns_when_blast_radius_empty_for_helper():
    """Empty blast_radius on a non-entry-point function adds an evidence note."""
    async def found(owner, repo, query):
        return [{"path": "src/x.js", "fragment": "function processOrder() {}"}]

    agent = _make_agent(found)
    result = DiagnosisResult(
        root_cause="x", confidence=0.9,
        affected_function="processOrder", affected_file="src/orders.js",
        blast_radius=[],
    )

    out = await agent._enforce_grounding(result)

    assert out.confidence == 0.9
    assert any("BLAST RADIUS WARNING" in e for e in out.evidence)


@pytest.mark.asyncio
async def test_grounding_skips_blast_radius_warning_for_entry_points():
    async def found(owner, repo, query):
        return [{"path": "src/routes/api.js", "fragment": "function paymentHandler() {}"}]

    agent = _make_agent(found)
    result = DiagnosisResult(
        root_cause="x", confidence=0.9,
        affected_function="paymentHandler", affected_file="src/routes/api.js",
        blast_radius=[],
    )

    out = await agent._enforce_grounding(result)
    assert all("BLAST RADIUS WARNING" not in e for e in out.evidence)


@pytest.mark.asyncio
async def test_grounding_skips_blast_radius_warning_when_evidence_explains():
    async def found(owner, repo, query):
        return [{"path": "src/x.js", "fragment": "function processOrder() {}"}]

    agent = _make_agent(found)
    result = DiagnosisResult(
        root_cause="x", confidence=0.9,
        affected_function="processOrder", affected_file="src/x.js",
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
        root_cause="x", confidence=0.9,
        affected_function="processOrder", affected_file="src/x.js",
        blast_radius=[{"file": "src/api.js", "function": "submit", "snippet": "processOrder()"}],
    )

    out = await agent._enforce_grounding(result)
    assert all("BLAST RADIUS WARNING" not in e for e in out.evidence)


@pytest.mark.asyncio
async def test_entry_point_helper_name_hints():
    """Spot-check the entry-point name detector."""
    from app.agents.diagnosis import _looks_like_entry_point

    assert _looks_like_entry_point("paymentHandler", [])
    assert _looks_like_entry_point("getUserRoute", [])
    assert _looks_like_entry_point("nightlyCron", [])
    assert _looks_like_entry_point("queueWorker", [])

    assert not _looks_like_entry_point("processOrder", [])
    assert not _looks_like_entry_point("validateInput", [])

    assert _looks_like_entry_point("processOrder", ["this is the express handler — no callers"])
