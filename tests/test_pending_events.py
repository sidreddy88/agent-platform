from app.models.events import ErrorEvent, EventSource
from app.services.pending_events import PendingEventStore, classify_handling


def _categorised_event(error_type: str, category: str = "error") -> ErrorEvent:
    return ErrorEvent(
        source=EventSource.CLOUDWATCH,
        error_type=error_type,
        title=f"{error_type} in svc",
        description=f"{error_type}: sample message",
        service="svc",
        category=category,
        metadata={"log_group": "/ecs/svc"},
    )


def test_category_passes_through_to_serialized_form() -> None:
    """ErrorEvent.category must surface in PendingEventStore.serialize()."""
    store = PendingEventStore()

    err_pe, _ = store.add(_categorised_event("APICONNECTIONTIMEOUTERROR", category="non_error"))
    bug_pe, _ = store.add(_categorised_event("TYPEERROR", category="error"))

    assert err_pe is not None and bug_pe is not None
    assert store.serialize(err_pe)["category"] == "non_error"
    assert store.serialize(bug_pe)["category"] == "error"


def test_category_defaults_to_error_when_event_omits_it() -> None:
    """Events constructed without an explicit category land in the Errors tab."""
    store = PendingEventStore()
    # ErrorEvent model default is "error" — verify it propagates.
    pe, _ = store.add(_categorised_event("ECS_ERROR"))

    assert pe is not None
    assert store.serialize(pe)["category"] == "error"


def _cloudwatch_event(
    timestamp: int,
    task_id: str = "task-a",
    description: str = "Error processing lead 123",
) -> ErrorEvent:
    return ErrorEvent(
        source=EventSource.CLOUDWATCH,
        error_type="ECS_ERROR",
        title="ECS_ERROR in api",
        description=description,
        service="api",
        resource_id="/ecs/api",
        metadata={
            "log_group": "/ecs/api",
            "task_id": task_id,
            "timestamp": timestamp,
        },
    )


def test_cloudwatch_pending_events_are_distinct_by_log_entry() -> None:
    store = PendingEventStore()

    first = store.add(_cloudwatch_event(timestamp=1000))
    second = store.add(_cloudwatch_event(timestamp=2000))

    assert first is not None
    assert second is not None
    assert first.id != second.id
    assert len(store.list_all()) == 2


def test_cloudwatch_pending_events_dedup_exact_same_log_entry() -> None:
    store = PendingEventStore()

    first = store.add(_cloudwatch_event(timestamp=1000))
    duplicate = store.add(_cloudwatch_event(timestamp=1000))

    assert first is not None
    assert duplicate is first
    assert len(store.list_all()) == 1


def test_forget_matching_lets_a_deleted_incidents_crash_resurface() -> None:
    """Regression test for a real production bug: a human deleted an incident,
    the exact same crash recurred the next day, and a crash scan still reported
    "no new crashes" because the pending-events dedup signature survived the
    incident deletion. forget_matching() must reset it so the next occurrence
    is treated as new.
    """
    store = PendingEventStore()

    first = store.add(_cloudwatch_event(timestamp=1000, description="CastError: previewCode"))
    assert first[1] is True  # first sighting is new

    # Human deletes the incident this pending event was promoted into.
    forgotten = store.forget_matching(first[0]._event)
    assert forgotten is not None
    assert forgotten.id == first[0].id
    assert store.list_all() == []

    # The exact same crash recurs (different timestamp) — must be treated as new,
    # not silently absorbed into a now-nonexistent incident's dedup record.
    recurrence = store.add(_cloudwatch_event(timestamp=2000, description="CastError: previewCode"))
    assert recurrence[1] is True
    assert recurrence[0].id != first[0].id


def test_forget_matching_is_a_noop_when_signature_unknown() -> None:
    store = PendingEventStore()
    assert store.forget_matching(_cloudwatch_event(timestamp=1000)) is None


def test_forget_matching_does_not_dismiss_the_signature() -> None:
    """Unlike dismiss(), forgetting must NOT block the signature from resurfacing —
    the whole point is to let it come back as a fresh, visible event."""
    store = PendingEventStore()
    pe, _ = store.add(_cloudwatch_event(timestamp=1000))
    store.forget_matching(pe._event)

    again = store.add(_cloudwatch_event(timestamp=1000))
    assert again[1] is True  # not suppressed like dismiss() would


def test_cloudwatch_same_timestamp_different_message_are_distinct() -> None:
    """Two distinct errors emitted at the same millisecond must not collapse to one."""
    store = PendingEventStore()

    first = store.add(_cloudwatch_event(timestamp=1000, description="TypeError: cannot read property"))
    second = store.add(_cloudwatch_event(timestamp=1000, description="ValueError: invalid input"))

    assert first is not None
    assert second is not None
    assert first.id != second.id
    assert len(store.list_all()) == 2


# ---------------------------------------------------------------------------
# classify_handling — caught vs uncaught
# ---------------------------------------------------------------------------

def test_classify_winston_envelope_with_stack_is_caught() -> None:
    """The 'Answers contain html tags' shape — winston envelope around an Error.

    Has 2 JS stack frames, but the leading 'error:' level prefix and the
    'Error-> processPosts Error: {' tag prove it's logger output, not Node's
    own uncaught preamble. Pre-fix this was misclassified as uncaught.
    """
    msg = (
        "error: Error: Answers contain html tags\n"
        "Error-> processPosts Error:  {\n"
        "    error: Error: Answers contain html tags\n"
        "        at Object.processPosts (/app/routes/api/interviewUsers.js:2323:21)\n"
        "        at process.processTicksAndRejections (node:internal/process/task_queues:95:5),\n"
        "    app: 'inspiring',\n"
        "    user: { _id: '...', email: 'x@y.com' }\n"
        "}"
    )
    label, evidence = classify_handling(msg)
    assert label == "caught", f"got {label}: {evidence}"


def test_classify_genuine_unhandled_promise_rejection_is_uncaught() -> None:
    """Real Node uncaught rejection — has the explicit preamble."""
    msg = (
        "node:internal/process/promises:288\n"
        "            triggerUncaughtException(err, true /* fromPromise */);\n"
        "            ^\n\n"
        "Error: Database connection lost\n"
        "    at connect (/app/db.js:42:9)\n"
        "    at Object.<anonymous> (/app/index.js:10:1)\n"
        "Node.js v20.10.0"
    )
    label, _ = classify_handling(msg)
    assert label == "uncaught"


def test_classify_python_traceback_is_uncaught() -> None:
    msg = (
        'Traceback (most recent call last):\n'
        '  File "/app/main.py", line 42, in handler\n'
        '    do_work()\n'
        '  File "/app/work.py", line 7, in do_work\n'
        '    raise ValueError("bad input")\n'
        'ValueError: bad input'
    )
    label, _ = classify_handling(msg)
    assert label == "uncaught"


def test_classify_bare_stack_without_envelope_is_unknown() -> None:
    """No logger envelope, no Node preamble — we don't know. Don't guess uncaught."""
    msg = (
        "Error: Something failed\n"
        "    at foo (/app/x.js:10:5)\n"
        "    at bar (/app/y.js:20:5)"
    )
    label, evidence = classify_handling(msg)
    assert label == "unknown", f"got {label}: {evidence}"


def test_classify_explicit_caught_marker_wins() -> None:
    msg = "ERROR: caught error in retry loop, will retry in 5s"
    label, _ = classify_handling(msg)
    assert label == "caught"


def test_classify_logger_prefix_no_stack_is_caught() -> None:
    msg = "error: payment validation failed for order 12345"
    label, _ = classify_handling(msg)
    assert label == "caught"


def test_classify_function_name_prefix_is_caught() -> None:
    """The applyModification/sharp.extract NaN log shape — caught despite no
    object envelope and no `error:` level prefix. The signal is the leading
    function name before `Error:` on the first line, which Node never produces
    on its own.
    """
    msg = (
        "applyModification Error: Expected integer for left but received NaN of type number\n"
        "    at Object.invalidParameterError (/app/node_modules/sharp/lib/is.js:135:10)\n"
        "    at Sharp.<anonymous> (/app/node_modules/sharp/lib/resize.js:475:16)\n"
        "    at Array.forEach (<anonymous>)\n"
        "    at Sharp.extract (/app/node_modules/sharp/lib/resize.js:470:38)\n"
        "    at Object.applyModification (/app/routes/services/image.js:93:10)\n"
        "    at process.processTicksAndRejections (node:internal/process/task_queues:95:5)\n"
        "    at async /app/routes/api/image.js:91:51 inspiringinterviews "
        "inspiring/1776973540619-1776973540083_maria_pod"
    )
    label, evidence = classify_handling(msg)
    assert label == "caught", f"got {label}: {evidence}"


def test_classify_node_error_class_alone_is_not_envelope() -> None:
    """A plain `Error: ...` first line (Node's own output) must NOT match the
    function-name-prefix envelope — otherwise we'd misclassify real uncaught.
    """
    msg = (
        "Error: Database connection lost\n"
        "    at connect (/app/db.js:42:9)\n"
        "    at Object.<anonymous> (/app/index.js:10:1)"
    )
    label, _ = classify_handling(msg)
    # No envelope, no Node preamble, just stack frames → unknown (not caught, not uncaught).
    assert label == "unknown"


def test_classify_typeerror_alone_is_not_envelope() -> None:
    """TypeError: ... is Node-native, not a logger prefix."""
    msg = (
        "TypeError: Cannot read properties of undefined (reading 'foo')\n"
        "    at handler (/app/x.js:10:5)\n"
        "    at Object.<anonymous> (/app/y.js:20:5)"
    )
    label, _ = classify_handling(msg)
    assert label == "unknown"


def test_classify_tag_arrow_funcname_brace_is_caught() -> None:
    """The postWithRetry/AxiosError 504 shape — `Error-> funcName {` style.

    The tag-arrow-funcname pattern alone is the signal; the `{` immediately
    after instead of `: {` (as in the processPosts case) varies by logger
    style. Both shapes must classify as caught.
    """
    msg = (
        "Error-> postWithRetry {\n"
        "  exception: AxiosError: Request failed with status code 504\n"
        "      at settle (/app/node_modules/axios/dist/node/axios.cjs:1967:12)\n"
        "      at IncomingMessage.handleStreamEnd (/app/node_modules/axios/dist/node/axios.cjs:3066:11)\n"
        "      at IncomingMessage.emit (node:events:525:35)\n"
        "      at IncomingMessage.emit (node:domain:489:12)\n"
        "      at endReadableNT (node:internal/streams/readable:1359:12)\n"
        "      at process.processTicksAndRejections (node:internal/process/task_queues:82:21)\n"
        "      at Axios.request (/app/node_modules/axios/dist/node/axios.cjs:3877:41)"
    )
    label, evidence = classify_handling(msg)
    assert label == "caught", f"got {label}: {evidence}"
