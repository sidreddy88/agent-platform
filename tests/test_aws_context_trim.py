"""Tests for `_trim_to_stack_frames` in app/services/aws.py.

The helper exists to stop the context-line accumulator at the first line that
doesn't look like a stack frame. Without it, TargetApp's habit of dumping
user interview HTML right after some errors corrupted error descriptions with
unrelated content.
"""
from app.services.aws import _trim_to_stack_frames


def test_keeps_node_stack_frames() -> None:
    ctx = [
        "    at uploadImageByConfig (/app/routes/services/image.js:536:67)",
        "    at process.processTicksAndRejections (node:internal/process/task_queues:96:5)",
    ]
    assert _trim_to_stack_frames(ctx) == ctx


def test_stops_at_user_content_after_stack_frames() -> None:
    """The exact failure mode that prompted this helper."""
    ctx = [
        "    at uploadImageByConfig (/app/routes/services/image.js:536:67)",
        "    at process.processTicksAndRejections",
        "<p><strong>Tell us about yourself</strong></p>",
        "    at neverSeen (/app/foo.js:1:1)",   # past the cut, should not be kept
    ]
    assert _trim_to_stack_frames(ctx) == ctx[:2]


def test_returns_empty_when_first_line_is_user_content() -> None:
    ctx = [
        "<p><strong>Alright, tell us about your story</strong></p>",
        "    at foo",
    ]
    assert _trim_to_stack_frames(ctx) == []


def test_keeps_python_file_line() -> None:
    ctx = [
        '  File "/app/services/rag.py", line 42, in search',
        "Random log line about something unrelated",
    ]
    assert _trim_to_stack_frames(ctx) == ctx[:1]


def test_keeps_blank_lines_between_frames() -> None:
    ctx = [
        "    at first (/app/a.js:1:1)",
        "",
        "    at second (/app/b.js:2:2)",
    ]
    assert _trim_to_stack_frames(ctx) == ctx


def test_keeps_nested_error_class() -> None:
    """`Caused by:` and inner error class lines are valid continuations."""
    ctx = [
        "Caused by: TypeError: nested cause",
        "ValueError: another cause",
        "user log line not a frame",
    ]
    assert _trim_to_stack_frames(ctx) == ctx[:2]


def test_handles_empty_input() -> None:
    assert _trim_to_stack_frames([]) == []
