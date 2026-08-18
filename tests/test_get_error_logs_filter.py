"""
Tests for AWSService.get_error_logs()'s filter_pattern override.

Real production bug: a genuine crash from ~36 hours back never appeared in a
4-week "crashes only" scan. Root cause: get_error_logs()'s day-chunked fetch
caps total raw events at 400, shared across every category the (generic,
multi-term) filter pattern matches. A noisy recent day full of plain Errors/
DeprecationWarnings consumed the whole budget before the chunk loop ever
walked back far enough to reach the older, rarer "app crashed" line. The
crash-only scan endpoint now passes a pattern scoped to just "app crashed" so
its budget isn't spent on categories it immediately filters out anyway.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.services.aws import AWSService


def _service_with_mock_logs_client():
    service = AWSService.__new__(AWSService)
    mock_logs = MagicMock()
    mock_logs.filter_log_events.return_value = {"events": []}
    return service, mock_logs


def test_default_filter_pattern_matches_multiple_categories():
    """No override -> the existing broad, multi-category pattern (unchanged
    behavior for callers like the generic /scan endpoints)."""
    service, mock_logs = _service_with_mock_logs_client()
    with patch.object(AWSService, "_client", return_value=mock_logs):
        service.get_error_logs("/ecs/svc", minutes=60)

    kwargs = mock_logs.filter_log_events.call_args.kwargs
    pattern = kwargs["filterPattern"]
    assert '"ERROR"' in pattern
    assert '"DeprecationWarning"' in pattern
    assert '"app crashed"' in pattern


def test_filter_pattern_override_is_used_verbatim():
    """The crash scan's override must reach CloudWatch as-is, not get merged
    with or diluted by the generic pattern."""
    service, mock_logs = _service_with_mock_logs_client()
    with patch.object(AWSService, "_client", return_value=mock_logs):
        service.get_error_logs("/ecs/svc", minutes=60, filter_pattern='"app crashed"')

    kwargs = mock_logs.filter_log_events.call_args.kwargs
    assert kwargs["filterPattern"] == '"app crashed"'


def test_crash_only_override_excludes_generic_error_terms():
    """The whole point of the override: a crash-scoped pattern must NOT also
    match plain Errors/DeprecationWarnings, so the shared event budget isn't
    spent on categories the caller is about to discard anyway."""
    service, mock_logs = _service_with_mock_logs_client()
    with patch.object(AWSService, "_client", return_value=mock_logs):
        service.get_error_logs("/ecs/svc", minutes=60, filter_pattern='"app crashed"')

    pattern = mock_logs.filter_log_events.call_args.kwargs["filterPattern"]
    assert "DeprecationWarning" not in pattern
    assert '"ERROR"' not in pattern


# ---------------------------------------------------------------------------
# Wall-clock safety net — the day-chunk walk must never run unbounded
# ---------------------------------------------------------------------------
#
# Real production incident: with a narrow filter that legitimately matches
# nothing for weeks, len(raw_events) < _MAX_EVENTS stays true for the ENTIRE
# requested window, so every single day-chunk (up to 28 for a 4-week scan)
# gets queried sequentially -- each a blocking network call, run in-line
# inside an async route handler. This hung the whole app for 10+ minutes.

def test_stops_early_once_wall_clock_budget_is_exceeded():
    """A filter that never matches anything must not force all 28 day-chunks
    of a 4-week scan to be queried -- the deadline must cut it off."""
    service, mock_logs = _service_with_mock_logs_client()
    mock_logs.filter_log_events.return_value = {"events": []}  # never matches, never paginates

    # First monotonic() call sets the deadline; simulate several seconds
    # passing per chunk check thereafter so the 45s budget is exceeded well
    # before all 28 day-chunks a 4-week window would otherwise require,
    # without needing to actually sleep in the test.
    fake_clock = iter([0.0] + [i * 4.0 for i in range(1, 200)])
    with (
        patch.object(AWSService, "_client", return_value=mock_logs),
        patch("app.services.aws.time.monotonic", side_effect=lambda: next(fake_clock)),
    ):
        events = service.get_error_logs("/ecs/svc", minutes=40_320, filter_pattern='"app crashed"')

    assert events == []
    # 45s budget / ~1s per chunk check -> nowhere near the full 28 chunks a
    # 4-week window would otherwise require.
    assert mock_logs.filter_log_events.call_count < 28


def test_does_not_time_out_when_matches_exist_within_budget():
    """Sanity check the deadline doesn't fire on a normal, fast-completing call."""
    service, mock_logs = _service_with_mock_logs_client()
    mock_logs.filter_log_events.return_value = {
        "events": [{"timestamp": 1000, "message": "app crashed", "logStreamName": "s"}],
    }
    with (
        patch.object(AWSService, "_client", return_value=mock_logs),
        patch("app.services.aws.time.monotonic", return_value=0.0),
    ):
        events = service.get_error_logs("/ecs/svc", minutes=60, filter_pattern='"app crashed"', limit=1)

    assert len(events) == 1
