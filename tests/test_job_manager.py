"""Worker pool behavior, driven by a fake runner.

No FFmpeg, no yt-dlp, no Chrome: the runner is injected, which is what makes the
concurrency, cancellation, and failure paths testable offline.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from videotrack.core.events import DOWNLOAD_COMPLETED, PROGRESS, PipelineEvent, Progress, progress_event
from videotrack.core.executor import DownloadCancelled
from videotrack.core.models import BatchItem
from videotrack.core.options import PipelineOptions
from videotrack.jobs.bus import EventBus
from videotrack.jobs.manager import DuplicateJob, JobManager
from videotrack.jobs.models import JobStatus
from videotrack.jobs.store import JobStore

SETTLE_SECONDS = 3.0
POLL_SECONDS = 0.02


class _ManagerFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.output_dir = Path(self._temp.name)
        self.store = JobStore(":memory:")
        self.bus = EventBus()
        self.addCleanup(self.store.close)

    def build(self, runner, concurrency: int = 2) -> JobManager:
        manager = JobManager(
            store=self.store,
            bus=self.bus,
            options=PipelineOptions(output_dir=self.output_dir),
            concurrency=concurrency,
            runner=runner,
        )
        self.addCleanup(manager.shutdown)
        return manager

    def wait_for(self, predicate, timeout: float = SETTLE_SECONDS) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(POLL_SECONDS)
        return False

    def wait_for_status(self, job_id: str, *statuses: JobStatus) -> JobStatus:
        wanted = set(statuses)
        self.assertTrue(
            self.wait_for(lambda: self.store.get(job_id).status in wanted),
            f"job stayed {self.store.get(job_id).status.value}, wanted one of {[s.value for s in wanted]}",
        )
        return self.store.get(job_id).status

    def writing_runner(self):
        def runner(job, resolution, cancel, on_event):
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"data")
            on_event(PipelineEvent(DOWNLOAD_COMPLETED, {"path": str(out)}))
            return out

        return runner


class CompletionTests(_ManagerFixture):
    def test_a_submitted_job_runs_to_completion(self) -> None:
        manager = self.build(self.writing_runner())

        job = manager.submit("https://page.example.test/w/1")

        self.assertEqual(self.wait_for_status(job.id, JobStatus.COMPLETED), JobStatus.COMPLETED)
        self.assertEqual(self.store.get(job.id).percent, 100.0)

    def test_the_output_path_is_recorded(self) -> None:
        manager = self.build(self.writing_runner())

        job = manager.submit("https://page.example.test/w/1")
        self.wait_for_status(job.id, JobStatus.COMPLETED)

        self.assertTrue(Path(self.store.get(job.id).output_path).exists())

    def test_an_output_path_is_claimed_before_the_worker_starts(self) -> None:
        # Claiming up front is what stops two concurrent jobs racing on a name.
        blocked = threading.Event()

        def runner(job, resolution, cancel, on_event):
            blocked.wait(timeout=SETTLE_SECONDS)
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        job = manager.submit("https://page.example.test/w/1")

        self.assertIsNotNone(self.store.get(job.id).output_path)
        blocked.set()


class ConcurrencyTests(_ManagerFixture):
    def test_concurrency_never_exceeds_the_limit(self) -> None:
        lock = threading.Lock()
        peak = {"current": 0, "max": 0}

        def runner(job, resolution, cancel, on_event):
            with lock:
                peak["current"] += 1
                peak["max"] = max(peak["max"], peak["current"])
            time.sleep(0.15)
            with lock:
                peak["current"] -= 1
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner, concurrency=2)
        jobs = [manager.submit(f"https://page.example.test/w/{i}") for i in range(6)]

        self.assertTrue(
            self.wait_for(lambda: all(self.store.get(j.id).status == JobStatus.COMPLETED for j in jobs), 10.0)
        )
        self.assertLessEqual(peak["max"], 2)

    def test_lowering_concurrency_takes_effect_without_a_restart(self) -> None:
        manager = self.build(self.writing_runner(), concurrency=4)

        manager.set_concurrency(1)

        self.assertEqual(manager.concurrency, 1)

    def test_concurrency_is_clamped_to_a_sane_range(self) -> None:
        manager = self.build(self.writing_runner(), concurrency=2)

        manager.set_concurrency(0)
        self.assertEqual(manager.concurrency, 1)

        manager.set_concurrency(9999)
        self.assertLessEqual(manager.concurrency, 16)

    def test_raising_concurrency_again_restores_throughput(self) -> None:
        manager = self.build(self.writing_runner(), concurrency=1)

        manager.set_concurrency(3)

        self.assertEqual(manager.concurrency, 3)


class FailureTests(_ManagerFixture):
    def test_a_raising_runner_fails_the_job_with_its_error_text(self) -> None:
        def runner(job, resolution, cancel, on_event):
            raise RuntimeError("ffmpeg exploded")

        manager = self.build(runner)
        job = manager.submit("https://page.example.test/w/1")

        self.wait_for_status(job.id, JobStatus.FAILED)
        self.assertIn("ffmpeg exploded", self.store.get(job.id).error)

    def test_one_failing_job_does_not_affect_another(self) -> None:
        def runner(job, resolution, cancel, on_event):
            if job.url.endswith("2"):
                raise RuntimeError("boom")
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        good = manager.submit("https://page.example.test/w/1")
        bad = manager.submit("https://page.example.test/w/2")

        self.wait_for_status(good.id, JobStatus.COMPLETED)
        self.wait_for_status(bad.id, JobStatus.FAILED)


class CancellationTests(_ManagerFixture):
    def test_cancelling_a_running_job_signals_its_runner(self) -> None:
        started = threading.Event()
        saw_cancel = threading.Event()

        def runner(job, resolution, cancel, on_event):
            started.set()
            if cancel.wait(timeout=SETTLE_SECONDS):
                saw_cancel.set()
                raise DownloadCancelled("cancelled")
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        job = manager.submit("https://page.example.test/w/1")
        self.assertTrue(started.wait(timeout=SETTLE_SECONDS))

        self.assertTrue(manager.cancel(job.id))

        self.assertTrue(saw_cancel.wait(timeout=SETTLE_SECONDS))
        self.assertEqual(self.wait_for_status(job.id, JobStatus.CANCELLED), JobStatus.CANCELLED)

    def test_cancelling_an_unknown_job_reports_false(self) -> None:
        manager = self.build(self.writing_runner())

        self.assertFalse(manager.cancel("does-not-exist"))

    def test_cancelling_a_completed_job_reports_false(self) -> None:
        manager = self.build(self.writing_runner())
        job = manager.submit("https://page.example.test/w/1")
        self.wait_for_status(job.id, JobStatus.COMPLETED)

        self.assertFalse(manager.cancel(job.id))


class RetryTests(_ManagerFixture):
    def test_a_failed_job_can_be_retried_and_succeed(self) -> None:
        attempts = {"n": 0}

        def runner(job, resolution, cancel, on_event):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("first attempt fails")
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        job = manager.submit("https://page.example.test/w/1")
        self.wait_for_status(job.id, JobStatus.FAILED)

        manager.retry(job.id)

        self.assertEqual(self.wait_for_status(job.id, JobStatus.COMPLETED), JobStatus.COMPLETED)

    def test_retrying_clears_the_previous_error(self) -> None:
        def runner(job, resolution, cancel, on_event):
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        job = manager.submit("https://page.example.test/w/1")
        self.wait_for_status(job.id, JobStatus.COMPLETED)
        stored = self.store.get(job.id)
        stored.error = "old error"
        self.store.update(stored)

        manager.retry(job.id)
        self.wait_for_status(job.id, JobStatus.COMPLETED)

        self.assertIsNone(self.store.get(job.id).error)

    def test_retrying_an_unknown_job_returns_none(self) -> None:
        manager = self.build(self.writing_runner())

        self.assertIsNone(manager.retry("does-not-exist"))


class DuplicateTests(_ManagerFixture):
    def test_submitting_an_active_duplicate_is_rejected(self) -> None:
        blocked = threading.Event()

        def runner(job, resolution, cancel, on_event):
            blocked.wait(timeout=SETTLE_SECONDS)
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        manager.submit("https://page.example.test/w/1")

        with self.assertRaises(DuplicateJob):
            manager.submit("https://page.example.test/w/1")

        blocked.set()

    def test_a_different_format_of_the_same_url_is_allowed(self) -> None:
        blocked = threading.Event()

        def runner(job, resolution, cancel, on_event):
            blocked.wait(timeout=SETTLE_SECONDS)
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        manager.submit("https://page.example.test/w/1", format_id="137")

        second = manager.submit("https://page.example.test/w/1", format_id="22")

        self.assertIsNotNone(second)
        blocked.set()


class BatchTests(_ManagerFixture):
    def test_a_batch_queues_independent_jobs_sharing_an_id(self) -> None:
        manager = self.build(self.writing_runner())
        items = tuple(BatchItem(f"https://page.example.test/w/{i}", f"Clip {i}") for i in range(5))

        batch, jobs, skipped = manager.submit_batch(items, source_url="https://page.example.test/list")

        self.assertEqual(len(jobs), 5)
        self.assertEqual(skipped, [])
        self.assertTrue(all(job.batch_id == batch.id for job in jobs))
        self.assertTrue(
            self.wait_for(lambda: len(self.store.list(batch_id=batch.id, status=JobStatus.COMPLETED)) == 5, 10.0)
        )

    def test_a_partly_failing_batch_completes_the_rest(self) -> None:
        def runner(job, resolution, cancel, on_event):
            if job.url.endswith(("1", "3")):
                raise RuntimeError("boom")
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        items = tuple(BatchItem(f"https://page.example.test/w/{i}") for i in range(5))

        batch, jobs, _ = manager.submit_batch(items)

        self.assertTrue(
            self.wait_for(
                lambda: len(self.store.list(batch_id=batch.id, status=JobStatus.COMPLETED)) == 3, 10.0
            )
        )
        self.assertEqual(len(self.store.list(batch_id=batch.id, status=JobStatus.FAILED)), 2)

    def test_a_duplicate_inside_a_batch_is_skipped_not_fatal(self) -> None:
        blocked = threading.Event()

        def runner(job, resolution, cancel, on_event):
            blocked.wait(timeout=SETTLE_SECONDS)
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        items = (
            BatchItem("https://page.example.test/w/1"),
            BatchItem("https://page.example.test/w/1"),
            BatchItem("https://page.example.test/w/2"),
        )

        _, jobs, skipped = manager.submit_batch(items)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(skipped, ["https://page.example.test/w/1"])
        blocked.set()

    def test_items_without_a_url_are_ignored(self) -> None:
        manager = self.build(self.writing_runner())

        _, jobs, _ = manager.submit_batch([{"title": "no url"}, {"url": "https://page.example.test/w/1"}])

        self.assertEqual(len(jobs), 1)


class ProgressPlumbingTests(_ManagerFixture):
    def test_progress_events_update_the_job_record(self) -> None:
        def runner(job, resolution, cancel, on_event):
            on_event(progress_event(Progress(phase="downloading", percent=42.0, downloaded_bytes=1024)))
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        job = manager.submit("https://page.example.test/w/1")
        self.wait_for_status(job.id, JobStatus.COMPLETED)

        # Completion overwrites percent, so the byte count is the durable proof.
        self.assertEqual(self.store.get(job.id).downloaded_bytes, 1024)

    def test_an_unknown_percent_is_stored_as_none(self) -> None:
        recorded = []

        def runner(job, resolution, cancel, on_event):
            on_event(progress_event(Progress(phase="downloading", percent=None, downloaded_bytes=5)))
            recorded.append(self.store.get(job.id).percent)
            out = self.output_dir / f"{job.id}.mp4"
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        job = manager.submit("https://page.example.test/w/1")
        self.wait_for_status(job.id, JobStatus.COMPLETED)

        self.assertEqual(recorded, [None])

    def test_events_reach_a_subscriber(self) -> None:
        manager = self.build(self.writing_runner())
        subscriber = self.bus.subscribe()

        job = manager.submit("https://page.example.test/w/1")
        self.wait_for_status(job.id, JobStatus.COMPLETED)

        kinds = []
        while not subscriber.empty():
            event = subscriber.get_nowait()
            if event is not None:
                kinds.append(event.event.kind)

        self.assertIn("job_completed", kinds)


class RecoveryTests(_ManagerFixture):
    def test_recovery_reports_interrupted_jobs(self) -> None:
        manager = self.build(self.writing_runner())
        job = manager.submit("https://page.example.test/w/1")
        self.wait_for_status(job.id, JobStatus.COMPLETED)
        stored = self.store.get(job.id)
        stored.status = JobStatus.DOWNLOADING
        self.store.update(stored)

        recovered = manager.recover_interrupted()

        self.assertEqual([r.id for r in recovered], [job.id])
        self.assertEqual(self.store.get(job.id).status, JobStatus.INTERRUPTED)


if __name__ == "__main__":
    unittest.main()
