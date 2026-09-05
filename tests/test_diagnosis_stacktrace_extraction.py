"""
Tests for extract_stack_trace_paths — the deterministic fast path that pulls
this app's own file paths directly out of raw error text, so DiagnosisAgent
can skip search_codebase (an embedding search with a similarity threshold)
for the common case where the file is already spelled out in the stack trace.

Real motivation and real data: checked against 4 of the (now 6, after
pulling in 2 more real DiagnosisAgent cases from production) cases in
app/evals/diagnosis_regression.jsonl (real merged-fix incidents) — 3 of 4
have the exact ground-truth file (and sometimes function) sitting verbatim
in the trace; the 4th (a bare DeprecationWarning) has no trace at all and is
the case this intentionally returns empty for, so callers fall back to
grep_codebase/search_codebase unchanged.
"""
from __future__ import annotations

from app.agents.diagnosis import extract_stack_trace_paths


def test_extracts_file_and_function_from_named_frame():
    """'at fnName (/app/path.js:L:C)' — the common named-frame case."""
    text = (
        "TypeError: Cannot read properties of undefined (reading 'publish_decision')\n"
        "/app/constants/prankCheckerMain.js:296\n"
        '    needsLLMReview = llmRes.classification.publish_decision != "allow";\n'
        "    at checkPrankForInterview (/app/constants/prankCheckerMain.js:296:44)\n"
        "    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)"
    )
    result = extract_stack_trace_paths(text)
    assert result == [{"file": "constants/prankCheckerMain.js", "function": "checkPrankForInterview"}]


def test_bare_throw_site_line_does_not_blank_out_a_later_function_name():
    """Same file appears twice: once bare (no fn), once in a named 'at' frame.

    The bare occurrence comes first in the text -- must not permanently lock
    the function name to None once the named occurrence is seen later.
    """
    text = (
        "/app/constants/prankCheckerMain.js:296\n"
        "    at checkPrankForInterview (/app/constants/prankCheckerMain.js:296:44)"
    )
    result = extract_stack_trace_paths(text)
    assert result == [{"file": "constants/prankCheckerMain.js", "function": "checkPrankForInterview"}]


def test_async_frame_has_no_function_name():
    """'at async /app/path.js:L:C' — no named function, must not error or misparse."""
    text = "at async /app/routes/api/image.js:356:20"
    result = extract_stack_trace_paths(text)
    assert result == [{"file": "routes/api/image.js", "function": None}]


def test_filters_out_node_modules_paths():
    """node_modules is installed under /app too -- must not be mistaken for app code."""
    text = (
        "at deserializeAws_restXmlNoSuchKeyResponse "
        "(/app/node_modules/@aws-sdk/client-s3/dist-cjs/protocols/Aws_restXml.js:6155:23)\n"
        "at async /app/routes/api/image.js:356:20"
    )
    result = extract_stack_trace_paths(text)
    assert result == [{"file": "routes/api/image.js", "function": None}]


def test_no_stack_trace_returns_empty_list():
    """A bare warning with no location info at all -- callers must fall back to search."""
    text = (
        "(node:674) [MONGOOSE] DeprecationWarning: Mongoose: the `strictQuery` "
        "option will be switched back to `false` by default in Mongoose 7."
    )
    assert extract_stack_trace_paths(text) == []


def test_empty_and_none_input_returns_empty_list():
    assert extract_stack_trace_paths("") == []
    assert extract_stack_trace_paths(None) == []  # type: ignore[arg-type]


def test_dedupes_repeated_path_preserving_first_occurrence_order():
    text = (
        "at async /app/routes/api/image.js:356:20\n"
        "at handler (/app/config/authenticateToken.js:20:7)\n"
        "at retry (/app/routes/api/image.js:356:20)"
    )
    result = extract_stack_trace_paths(text)
    assert [r["file"] for r in result] == ["routes/api/image.js", "config/authenticateToken.js"]


def test_multiple_distinct_app_files_both_kept_in_order():
    """Also covers 'at async fnName (...)' -- the async-named-frame shape,
    distinct from both the plain-named frame and the no-name async frame."""
    text = (
        "at checkPrankForInterview (/app/constants/prankCheckerMain.js:296:44)\n"
        "at process.processTicksAndRejections (node:internal/process/task_queues:95:5)\n"
        "at async checkForLLMPrankInterview (/app/routes/services/interview-user-service.js:480:51)"
    )
    result = extract_stack_trace_paths(text)
    assert result == [
        {"file": "constants/prankCheckerMain.js", "function": "checkPrankForInterview"},
        {"file": "routes/services/interview-user-service.js", "function": "checkForLLMPrankInterview"},
    ]


def test_real_pipeline_regression_dataset_cases():
    """End-to-end sanity check against real merged-fix incidents, if the
    (gitignored, locally-populated) dataset is present. Skips cleanly in a
    fresh clone where the file doesn't exist yet.

    Keyed by incident_id, not title -- the dataset now has two different
    APP_CRASHED incidents (different real bugs, different ground-truth
    files) sharing the exact same title string. Keying by title alone would
    silently check whichever one happened to come first in the file against
    both expectations -- found this exact bug while updating this test for
    the dataset's growth from 4 to 6 real cases.
    """
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parent.parent / "app" / "evals" / "diagnosis_regression.jsonl"
    if not path.exists():
        return

    expectations = {
        "b90ed493-a21c-41fb-bb19-c95b9a11f15f": "constants/prankCheckerMain.js",   # TYPEERROR
        "c3c393e8-7187-4b21-a5f9-6b4e72f23c42": "constants/prankCheckerOpenAI.js", # SYNTAXERROR
        "323b34f6-1374-4cb9-b161-f3c4e5ea5412": "routes/api/crInterviewUsers.js",  # APP_CRASHED (CastError)
        "1b576622-9bb8-4be0-b9cb-8a2e7ea13edf": "config/authenticateToken.js",     # TOKENEXPIREDERROR
        "b46ae663-c0e7-4dca-90b2-8ba781baf9c4": None,  # DEPRECATIONWARNING -- no trace, must stay empty
        "b71aa206-2d7e-4236-bc91-980181d11773": "routes/api/image.js",            # APP_CRASHED (S3 NoSuchKey)
    }
    with path.open() as f:
        for line in f:
            case = json.loads(line)
            incident_id = case["incident_id"]
            if incident_id not in expectations:
                continue
            result = extract_stack_trace_paths(case["event"]["description"])
            expected_file = expectations[incident_id]
            if expected_file is None:
                assert result == [], f"{incident_id}: expected no extraction, got {result}"
            else:
                assert result and result[0]["file"] == expected_file, (
                    f"{incident_id}: expected first candidate {expected_file!r}, got {result}"
                )
