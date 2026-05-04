from app.models.events import ErrorEvent, EventSource
from app.services.pending_events import PendingEventStore


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


def test_cloudwatch_same_timestamp_different_message_are_distinct() -> None:
    """Two distinct errors emitted at the same millisecond must not collapse to one."""
    store = PendingEventStore()

    first = store.add(_cloudwatch_event(timestamp=1000, description="TypeError: cannot read property"))
    second = store.add(_cloudwatch_event(timestamp=1000, description="ValueError: invalid input"))

    assert first is not None
    assert second is not None
    assert first.id != second.id
    assert len(store.list_all()) == 2
