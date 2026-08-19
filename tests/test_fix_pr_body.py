"""
Regression tests for _build_fix_pr_body — the PR body FixGenerationAgent opens for
an auto-generated fix.

Real production bug, found on a live PR (TargetOrg/TargetApp#2589): the
inline version this replaced truncated the self-critique to 300 chars before
embedding it (`critique[:300]`), which cut the text off before the verdict line
the critique prompt itself asks for LAST ("FINAL LINE — must be exactly one of:
LOOKS CORRECT / NEEDS REVIEW / LIKELY WRONG"). A reviewer reading the PR could
never actually see whether the agent's own critique passed or failed the fix.
The critique is also itself markdown (Haiku often returns a full
"# Critique: ..." document despite being asked for a short assessment), and
embedding that inside a single `- **Self-critique:** ...` bullet broke GitHub's
rendering, since a heading can't nest inside a list item.
"""
from __future__ import annotations

from app.agents.fix_generation import _build_fix_pr_body


def _long_critique() -> str:
    # Mirrors the real PR's critique length and shape: five numbered answers,
    # verdict line last. > 300 chars, verdict nowhere near the front.
    return (
        "1. Does the fix break any Tier 2 caller? No — the only caller is "
        "processInterviews, which already wraps the call in try/catch and "
        "continues on error; the new code throws a more informative error "
        "instead of letting axios throw, which the existing catch still handles.\n"
        "2. Did the fix handle every edge case implied by the root cause? Partially — "
        "it prevents the wasted CDN round-trip but does not persist a marker so "
        "future runs skip a permanently-missing image.\n"
        "3. Is there a simpler fix? No.\n"
        "4. Does any other file need updating? No.\n"
        "5. Does the fix address the root cause or just suppress the error? It "
        "addresses it directly — checks existence before fetching.\n"
        "NEEDS REVIEW"
    )


def test_pr_body_includes_full_critique_not_truncated():
    critique = _long_critique()
    assert len(critique) > 300  # confirms this test actually exercises the bug

    body = _build_fix_pr_body(
        diagnosis="createThumbnailFromUrl calls axios.get with no existence check",
        function_name="createThumbnailFromUrl",
        file_path="routes/api/interviewUsers.js",
        critique=critique,
        issue_number=2588,
        incident_id="14f9bc16-fcb4-4f65-960b-6d7aab124dd7",
        confidence=0.55,
    )

    assert critique in body
    # The verdict line — the whole point of running a critique — must survive.
    assert "NEEDS REVIEW" in body


def test_pr_body_gives_critique_its_own_section():
    """Not crammed into the summary bullet list — a real section boundary, so a
    critique that itself contains markdown headers renders correctly instead of
    breaking out of a list item."""
    critique = "# Critique: some fix\n\n## Four Explicit Checks\n\nLOOKS CORRECT"

    body = _build_fix_pr_body(
        diagnosis="x",
        function_name="fn",
        file_path="a.js",
        critique=critique,
        issue_number=None,
        incident_id="abc-123",
        confidence=0.9,
    )

    assert "## Self-critique" in body
    assert "- **Self-critique:**" not in body
    assert critique in body


def test_pr_body_omits_fixes_line_when_no_issue_number():
    body = _build_fix_pr_body(
        diagnosis="x",
        function_name="fn",
        file_path="a.js",
        critique="LOOKS CORRECT",
        issue_number=None,
        incident_id="abc-123",
        confidence=0.9,
    )

    assert "Fixes #" not in body


def test_pr_body_includes_issue_link_when_present():
    body = _build_fix_pr_body(
        diagnosis="x",
        function_name="fn",
        file_path="a.js",
        critique="LOOKS CORRECT",
        issue_number=42,
        incident_id="abc-123",
        confidence=0.9,
    )

    assert "Fixes #42" in body


def test_pr_body_formats_confidence_as_percent():
    body = _build_fix_pr_body(
        diagnosis="x",
        function_name="fn",
        file_path="a.js",
        critique="LOOKS CORRECT",
        issue_number=None,
        incident_id="abc-123",
        confidence=0.55,
    )

    assert "55%" in body
