"""The event bus and the SSE plumbing.

The stream generator is exercised directly rather than through TestClient: a
synchronous test client blocks on an endless SSE response, and the route body is
deliberately trivial so testing the generator tests the behavior.
"""

from __future__ import annotations

import json
import queue
import threading
import unittest

from videotrack.core.events import PipelineEvent
from videotrack.jobs.bus import EventBus
from videotrack.jobs.models import JobEvent
from videotrack.server.routes import SSE_HEARTBEAT_SECONDS, _next_event


def _event(job_id: str = "job-1", kind: str = "progress") -> JobEvent:
    return JobEvent(job_id=job_id, event=PipelineEvent(kind, {"percent": 42.0}), created_at="now")


class BusTests(unittest.TestCase):
    def test_a_subscriber_receives_published_events(self) -> None:
        bus = EventBus()
        subscriber = bus.subscribe()

        bus.publish(_event())

        received = subscriber.get_nowait()
        self.assertEqual(received.job_id, "job-1")
        self.assertEqual(received.event.kind, "progress")

    def test_every_subscriber_receives_the_same_event(self) -> None:
        bus = EventBus()
        first, second = bus.subscribe(), bus.subscribe()

        bus.publish(_event())

        self.assertEqual(first.get_nowait().job_id, second.get_nowait().job_id)

    def test_unsubscribing_stops_delivery(self) -> None:
        bus = EventBus()
        subscriber = bus.subscribe()

        bus.unsubscribe(subscriber)
        bus.publish(_event())

        # Only the sentinel that ends the stream.
        self.assertIsNone(subscriber.get_nowait())
        self.assertTrue(subscriber.empty())

    def test_publishing_with_no_subscribers_is_harmless(self) -> None:
        EventBus().publish(_event())

    def test_a_full_subscriber_does_not_block_the_publisher(self) -> None:
        # A UI that stops draining must never stall the worker producing events.
        bus = EventBus(queue_size=2)
        bus.subscribe()

        for _ in range(50):
            bus.publish(_event())

    def test_the_subscriber_count_is_reported(self) -> None:
        bus = EventBus()
        bus.subscribe()
        bus.subscribe()

        self.assertEqual(bus.subscriber_count, 2)

    def test_closing_ends_every_stream(self) -> None:
        bus = EventBus()
        subscriber = bus.subscribe()

        bus.close()

        self.assertIsNone(subscriber.get_nowait())
        self.assertEqual(bus.subscriber_count, 0)

    def test_events_published_from_another_thread_arrive(self) -> None:
        bus = EventBus()
        subscriber = bus.subscribe()

        thread = threading.Thread(target=lambda: bus.publish(_event("from-thread")))
        thread.start()
        thread.join()

        self.assertEqual(subscriber.get(timeout=2.0).job_id, "from-thread")


class StreamHelperTests(unittest.TestCase):
    def test_an_available_event_is_returned(self) -> None:
        subscriber: queue.Queue = queue.Queue()
        subscriber.put(_event())

        self.assertIsNotNone(_next_event(subscriber))

    def test_an_idle_wait_returns_none_so_the_caller_can_heartbeat(self) -> None:
        import videotrack.server.routes as routes

        subscriber: queue.Queue = queue.Queue()
        original = routes.SSE_HEARTBEAT_SECONDS
        routes.SSE_HEARTBEAT_SECONDS = 0.01
        try:
            self.assertIsNone(_next_event(subscriber))
        finally:
            routes.SSE_HEARTBEAT_SECONDS = original

    def test_the_heartbeat_interval_is_short_enough_for_proxies(self) -> None:
        self.assertLessEqual(SSE_HEARTBEAT_SECONDS, 30.0)


class SerializationTests(unittest.TestCase):
    def test_an_event_serializes_to_json_for_the_wire(self) -> None:
        payload = json.dumps(_event().to_dict())

        parsed = json.loads(payload)
        self.assertEqual(parsed["job_id"], "job-1")
        self.assertEqual(parsed["kind"], "progress")
        self.assertEqual(parsed["payload"]["percent"], 42.0)

    def test_an_unknown_percent_serializes_as_null(self) -> None:
        event = JobEvent(job_id="j", event=PipelineEvent("progress", {"percent": None}))

        parsed = json.loads(json.dumps(event.to_dict()))

        self.assertIsNone(parsed["payload"]["percent"])

    def test_the_batch_id_travels_with_the_event(self) -> None:
        event = JobEvent(job_id="j", batch_id="b", event=PipelineEvent("progress", {}))

        self.assertEqual(json.loads(json.dumps(event.to_dict()))["batch_id"], "b")


if __name__ == "__main__":
    unittest.main()
