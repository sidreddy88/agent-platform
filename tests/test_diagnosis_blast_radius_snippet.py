"""
Regression tests for DiagnosisAgent's blast_radius snippet verification.

Real production bug: a diagnosis correctly read brandCInterviewUsers.js and found its
real, still-vulnerable previewCode handler. It then listed 3 sibling files
(brandAInterviewUsers.js, cityNationalInterviewUsers.js,
smallBusinessOfTheDayInterviewUsers.js) as having "the identical missing guard",
each with a detailed, plausible-looking snippet -- the brandCInterviewUsers.js snippet
with the Mongoose model name swapped. All 3 files are real (existing checks caught
nothing), but all 3 snippets were completely fabricated: those files were fixed via
earlier, separate incidents and no longer contain anything resembling that code.

_file_exists_in_repo only verifies the FILE is real. This adds a second check --
_snippet_is_grounded -- that verifies the snippet's actual code shape is present in
that file, tolerant of the one thing that legitimately differs between real sibling
files (the model class name) via _snippet_skeleton.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.diagnosis import DiagnosisAgent, DiagnosisResult, _snippet_skeleton


def _make_agent(file_contents: dict[str, str]) -> DiagnosisAgent:
    agent = DiagnosisAgent.__new__(DiagnosisAgent)
    agent._owner = "owner"
    agent._repo = "repo"
    agent._aws = MagicMock()
    agent._rag = None
    agent._github = MagicMock()
    agent._github.search_code = AsyncMock(return_value=[])
    agent._local_repo = MagicMock()
    agent._local_repo.ready = True
    agent._local_repo.file_exists = MagicMock(side_effect=lambda p: p in file_contents)
    agent._local_repo.read_file = MagicMock(side_effect=lambda p: file_contents[p])
    return agent


# ---------------------------------------------------------------------------
# _snippet_skeleton
# ---------------------------------------------------------------------------

def test_snippet_skeleton_strips_pascal_case_identifiers():
    a = _snippet_skeleton("await MasterBrandA.findOne({ previewCode: Number(previewCode) })")
    b = _snippet_skeleton("await MasterBrandC.findOne({ previewCode: Number(previewCode) })")
    assert a == b  # only the model name differs -- must normalize to the same skeleton


def test_snippet_skeleton_collapses_whitespace():
    assert _snippet_skeleton("a   b\n\nc") == _snippet_skeleton("a b c")


# ---------------------------------------------------------------------------
# _enforce_grounding — blast_radius snippet verification (integration)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fabricated_sibling_snippet_is_dropped():
    """The exact production failure: a real file, with a fabricated snippet that
    doesn't match its actual (already-fixed) content, must be removed."""
    real_snippet = (
        "router.get('/preview/:previewCode', authenticateToken, async (req, res) => {\n"
        "  const { previewCode } = req.params;\n"
        "  const interview = await MasterBrandC.findOne({ previewCode: Number(previewCode) });\n"
        "  res.json(interview);\n"
        "});"
    )
    fabricated_snippet = real_snippet.replace("MasterBrandC", "MasterBrandA")
    # brandAInterviewUsers.js is REAL, but its actual content is nothing like the
    # fabricated snippet -- already fixed via a separate, earlier incident.
    actual_brandA_content = (
        'router.get("/getPreviewUser/:id", (req, res) => {\n'
        '  const { id } = req.params;\n'
        '  if (!/^\\d+$/.test(id)) {\n'
        '    return res.status(400).json({ message: "Invalid previewCode" });\n'
        '  }\n'
        '  BrandAInterviewUser.find({ previewCode: Number(id) }).then((users) => {\n'
        '      res.json(users);\n'
        '  }).catch(() => {\n'
        '    res.status(500).json({ message: "Internal server error" });\n'
        '  });\n'
        '});'
    )
    agent = _make_agent({
        "routes/api/brandCInterviewUsers.js": real_snippet,
        "routes/api/brandAInterviewUsers.js": actual_brandA_content,
    })
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        blast_radius=[
            {"file": "routes/api/brandCInterviewUsers.js", "function": "(handler)", "snippet": real_snippet},
            {"file": "routes/api/brandAInterviewUsers.js", "function": "(handler)", "snippet": fabricated_snippet},
        ],
    )

    out = await agent._enforce_grounding(result)

    files = [e["file"] for e in out.blast_radius]
    assert "routes/api/brandCInterviewUsers.js" in files       # real snippet kept
    assert "routes/api/brandAInterviewUsers.js" not in files  # fabricated snippet dropped
    assert any("brandAInterviewUsers.js" in e for e in out.evidence)


@pytest.mark.asyncio
async def test_snippet_matching_only_by_model_name_is_kept():
    """A genuine sibling with the SAME real bug, differing only by model name
    (the actual legitimate case this whole feature exists to fix), must be kept."""
    snippet_a = "await MasterBrandC.findOne({ previewCode: Number(previewCode) });"
    snippet_b = "await MasterBrandB.findOne({ previewCode: Number(previewCode) });"
    agent = _make_agent({
        "routes/api/brandCInterviewUsers.js": f"router.get('/x', async (req,res) => {{ {snippet_a} }});",
        "routes/api/brandBInterviewUsers.js": f"router.get('/x', async (req,res) => {{ {snippet_b} }});",
    })
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        blast_radius=[
            {"file": "routes/api/brandCInterviewUsers.js", "function": "(handler)", "snippet": snippet_a},
            {"file": "routes/api/brandBInterviewUsers.js", "function": "(handler)", "snippet": snippet_b},
        ],
    )

    out = await agent._enforce_grounding(result)

    files = [e["file"] for e in out.blast_radius]
    assert "routes/api/brandCInterviewUsers.js" in files
    assert "routes/api/brandBInterviewUsers.js" in files


@pytest.mark.asyncio
async def test_short_snippet_is_not_verified():
    """Avoid false positives on trivially short snippets that could coincidentally
    substring-match unrelated file content."""
    agent = _make_agent({"routes/api/x.js": "totally unrelated file content"})
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        blast_radius=[{"file": "routes/api/x.js", "function": "(handler)", "snippet": "res.json()"}],
    )

    out = await agent._enforce_grounding(result)

    assert len(out.blast_radius) == 1  # kept — too short to reliably verify


@pytest.mark.asyncio
async def test_missing_snippet_skips_verification():
    """An entry with no snippet at all only goes through the existing file-exists
    check, unaffected by this new verification."""
    agent = _make_agent({"routes/api/x.js": "anything"})
    result = DiagnosisResult(
        root_cause="x",
        confidence=0.9,
        blast_radius=[{"file": "routes/api/x.js", "function": "(handler)", "snippet": ""}],
    )

    out = await agent._enforce_grounding(result)

    assert len(out.blast_radius) == 1
