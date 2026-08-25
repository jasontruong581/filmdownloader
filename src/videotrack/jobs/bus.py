"""In-process publish/subscribe for job events.

Live events only. There is deliberately no replay buffer: every consumer
reconciles against the job store on connect and on reconnect, and that snapshot
already guarantees a missed event cannot leave a stale view. A second history
mechanism would be a second thing to keep consistent.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator

from .models import JobEvent

#: Bound on a single subscriber's backlog. A subscriber that stops draining is
#: dropped rather than allowed to grow without limit.
SUBSCRIBER_QUEUE_SIZE = 512


class EventBus:
    def __init__(self, queue_size: int = SUBSCRIBER_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue[JobEvent | None]] = []

    def publish(self, event: JobEvent) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # A subscriber that cannot keep up loses events rather than
                # blocking the worker that produced them.
                pass

    def subscribe(self) -> "queue.Queue[JobEvent | None]":
        subscriber: queue.Queue[JobEvent | None] = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: "queue.Queue[JobEvent | None]") -> None:
        with self._lock:
            if subscriber in self._subscribers:
                self._subscribers.remove(subscriber)
        try:
            subscriber.put_nowait(None)
        except queue.Full:
            pass

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def stream(self, subscriber: "queue.Queue[JobEvent | None]", timeout: float = 15.0) -> Iterator[JobEvent | None]:
        """Yield events, or None on each idle timeout so a caller can heartbeat."""
        while True:
            try:
                event = subscriber.get(timeout=timeout)
            except queue.Empty:
                yield None
                continue
            if event is None:
                return
            yield event

    def close(self) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
            self._subscribers.clear()
        for subscriber in subscribers:
            try:
                subscriber.put_nowait(None)
            except queue.Full:
                pass
