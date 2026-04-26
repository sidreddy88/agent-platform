"""
Event queue — async queue for ErrorEvents flowing from detection layer to agents.
"""
import asyncio

from app.models.events import ErrorEvent


class EventQueue:
    def __init__(self, maxsize: int = 1000):
        self._queue: asyncio.Queue[ErrorEvent] = asyncio.Queue(maxsize=maxsize)
        self._total_enqueued = 0
        self._total_dequeued = 0

    async def enqueue(self, event: ErrorEvent) -> None:
        await self._queue.put(event)
        self._total_enqueued += 1

    async def dequeue(self) -> ErrorEvent:
        event = await self._queue.get()
        self._total_dequeued += 1
        return event

    def enqueue_nowait(self, event: ErrorEvent) -> None:
        self._queue.put_nowait(event)
        self._total_enqueued += 1

    def task_done(self) -> None:
        self._queue.task_done()

    @property
    def size(self) -> int:
        return self._queue.qsize()

    @property
    def stats(self) -> dict:
        return {
            "queue_size": self.size,
            "total_enqueued": self._total_enqueued,
            "total_dequeued": self._total_dequeued,
        }


# Module-level singleton
event_queue = EventQueue()
