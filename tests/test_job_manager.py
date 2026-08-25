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
from unittest.mock import patch

from videotrack.core.events import DOWNLOAD_COMPLETED, PROGRESS, PipelineEvent, Progress, progress_event
from videotrack.core.executor import DownloadCancelled
from videotrack.core.models import BatchItem, CaptureResult, NetworkRequest, StreamCandidate
from videotrack.core.options import PipelineOptions
from videotrack.core.resolvers import Resolution
from videotrack.jobs.bus import EventBus
from videotrack.jobs.manager import DuplicateJob, JobManager
from videotrack.jobs.models import JobStatus
from videotrack.jobs.store import JobStore

SETTLE_SECONDS = 3.0
POLL_SECONDS = 0.02
#: Escape hatch for a runner a test holds open. Deliberately far longer than any
#: window a test measures against, so a slow machine cannot let a runner finish
#: early and turn a timing assertion into a false failure. It only bounds a hang.
RUNNER_GUARD_SECONDS = 60.0


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


    def test_two_jobs_with_the_same_title_claim_distinct_paths(self) -> None:
        # Regression: the claim consulted only the filesystem, and nothing is
        # written at submit time, so both jobs saw the same free name. Under
        # `ffmpeg -y` two workers would then write one file.
        blocked = threading.Event()

        def runner(job, resolution, cancel, on_event):
            blocked.wait(timeout=SETTLE_SECONDS)
            out = Path(job.output_path)
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        first = manager.submit("https://page.example.test/w/1", title="Same Title")
        second = manager.submit("https://page.example.test/w/2", title="Same Title")

        self.assertNotEqual(first.output_path, second.output_path)

        blocked.set()
        self.assertTrue(
            self.wait_for(
                lambda: all(
                    self.store.get(job.id).status == JobStatus.COMPLETED
                    for job in (first, second)
                )
            )
        )
        self.assertTrue(Path(first.output_path).exists())
        self.assertTrue(Path(second.output_path).exists())


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


    def test_lowering_concurrency_while_every_slot_is_held_does_not_block(self) -> None:
        # Regression: the change waited on a blocking acquire while holding the
        # manager lock, so `PUT /api/settings` hung until a job finished, and
        # cancel() and shutdown() hung behind the same lock.
        release = threading.Event()
        entered = threading.Semaphore(0)

        def runner(job, resolution, cancel, on_event):
            entered.release()
            release.wait(timeout=RUNNER_GUARD_SECONDS)
            out = Path(job.output_path)
            out.write_bytes(b"x")
            return out

        manager = self.build(runner, concurrency=2)
        first = manager.submit("https://page.example.test/w/1")
        manager.submit("https://page.example.test/w/2")
        self.assertTrue(entered.acquire(timeout=SETTLE_SECONDS))
        self.assertTrue(entered.acquire(timeout=SETTLE_SECONDS))

        changed = threading.Event()

        def lower() -> None:
            manager.set_concurrency(1)
            changed.set()

        threading.Thread(target=lower, daemon=True).start()

        self.assertTrue(changed.wait(timeout=2.0), "set_concurrency blocked while slots were held")
        self.assertEqual(manager.concurrency, 1)
        # The lock it used to hold is the one cancel() and shutdown() need.
        self.assertTrue(manager.cancel(first.id))
        release.set()

    def test_an_excess_job_is_not_admitted_after_the_limit_drops(self) -> None:
        running = []
        release = threading.Event()
        lock = threading.Lock()

        def runner(job, resolution, cancel, on_event):
            with lock:
                running.append(job.id)
            release.wait(timeout=RUNNER_GUARD_SECONDS)
            out = Path(job.output_path)
            out.write_bytes(b"x")
            return out

        manager = self.build(runner, concurrency=2)
        manager.submit("https://page.example.test/w/1")
        manager.submit("https://page.example.test/w/2")
        self.assertTrue(self.wait_for(lambda: len(running) == 2))

        manager.set_concurrency(1)
        manager.submit("https://page.example.test/w/3")

        # The two in flight are allowed to finish, but nothing replaces them.
        self.assertFalse(self.wait_for(lambda: len(running) > 2, timeout=0.5))
        release.set()


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


class DiscoveryDelegationTests(_ManagerFixture):
    """The job runner has to find media the same way the CLI does.

    A page that serves its stream from an embed yields no candidate on the outer
    capture. Only the deep scan in the pipeline reaches the embed, so a runner
    that calls detect_candidates by itself fails on exactly the pages the CLI
    handles. These drive the real `_default_runner`, not an injected one.
    """

    def outer_capture(self) -> CaptureResult:
        # A player frame, and deliberately no media request at this level.
        return CaptureResult(
            page_url="https://page.example.test/watch/1",
            final_url="https://page.example.test/watch/1",
            title="Embedded Clip",
            user_agent="test-agent",
            cookies={},
            requests=[
                NetworkRequest(
                    url="https://frame.example.test/embed/abc",
                    method="GET",
                    headers={},
                    resource_type="Document",
                    status=200,
                )
            ],
        )

    def embed_capture(self, *media_urls: str) -> CaptureResult:
        return CaptureResult(
            page_url="https://frame.example.test/embed/abc",
            final_url="https://frame.example.test/embed/abc",
            title="Embedded Clip",
            user_agent="test-agent",
            cookies={},
            requests=[
                NetworkRequest(url=url, method="GET", headers={}, resource_type="Media", status=200)
                for url in media_urls
            ],
        )

    def discovering_manager(self) -> JobManager:
        """A manager on the real runner, with every network-touching step off."""
        manager = JobManager(
            store=self.store,
            bus=self.bus,
            options=PipelineOptions(
                output_dir=self.output_dir,
                probe=False,
                precheck_hls=False,
                rank_with_ffprobe=False,
                min_duration=0,
            ),
            concurrency=1,
        )
        self.addCleanup(manager.shutdown)
        return manager

    def browser_resolution(self, capture: CaptureResult) -> Resolution:
        return Resolution(
            resolver="browser",
            page_url=capture.page_url,
            final_url=capture.final_url,
            title=capture.title,
            media=(),
            engine="browser",
            capture=capture,
        )

    def recording_executor(self, calls: list, fail_urls: frozenset = frozenset()):
        class _Executor:
            name = "ffmpeg"

            def __init__(self, duration_hint=None):
                self.duration_hint = duration_hint

            def run(self, request, cancel, on_event):
                calls.append((request.candidate.url, self.duration_hint))
                if request.candidate.url in fail_urls:
                    raise RuntimeError("cdn refused this candidate")
                request.out_file.parent.mkdir(parents=True, exist_ok=True)
                request.out_file.write_bytes(b"media")
                return request.out_file

        return _Executor

    def test_media_found_only_inside_an_embed_still_downloads(self) -> None:
        calls: list = []
        manager = self.discovering_manager()
        resolution_id = manager.cache.put(self.browser_resolution(self.outer_capture()))
        stream = "https://cdn.example.test/hls/master.m3u8"

        with patch(
            "videotrack.core.pipeline.capture_page", return_value=self.embed_capture(stream)
        ), patch("videotrack.core.ffmpeg_executor.FfmpegExecutor", self.recording_executor(calls)):
            job = manager.submit("https://page.example.test/watch/1", resolution_id=resolution_id)
            self.wait_for_status(job.id, JobStatus.COMPLETED, JobStatus.FAILED)

        record = self.store.get(job.id)
        self.assertEqual(record.status, JobStatus.COMPLETED, record.error)
        self.assertEqual([url for url, _hint in calls], [stream])

    def test_a_probed_duration_reaches_the_executor_as_a_hint(self) -> None:
        # Regression: the executor was built with no hint, so percent stayed None
        # for the whole download even where the duration was already known.
        calls: list = []
        manager = self.discovering_manager()
        resolution_id = manager.cache.put(self.browser_resolution(self.outer_capture()))
        candidate = StreamCandidate(
            url="https://cdn.example.test/hls/master.m3u8",
            kind="hls",
            score=10,
            source="main",
            probe_duration=612.0,
        )

        with patch(
            "videotrack.core.pipeline.prepare_candidates",
            return_value=([candidate], {}, [candidate]),
        ), patch("videotrack.core.ffmpeg_executor.FfmpegExecutor", self.recording_executor(calls)):
            job = manager.submit("https://page.example.test/watch/1", resolution_id=resolution_id)
            self.wait_for_status(job.id, JobStatus.COMPLETED, JobStatus.FAILED)

        self.assertEqual(self.store.get(job.id).status, JobStatus.COMPLETED)
        self.assertEqual([hint for _url, hint in calls], [612.0])

    def test_a_failing_candidate_falls_through_to_the_next(self) -> None:
        calls: list = []
        manager = self.discovering_manager()
        resolution_id = manager.cache.put(self.browser_resolution(self.outer_capture()))
        first = "https://cdn.example.test/hls/first.m3u8"
        second = "https://cdn.example.test/hls/second.m3u8"

        with patch(
            "videotrack.core.pipeline.capture_page",
            return_value=self.embed_capture(first, second),
        ), patch(
            "videotrack.core.ffmpeg_executor.FfmpegExecutor",
            self.recording_executor(calls, fail_urls=frozenset({first})),
        ):
            job = manager.submit("https://page.example.test/watch/1", resolution_id=resolution_id)
            self.wait_for_status(job.id, JobStatus.COMPLETED, JobStatus.FAILED)

        record = self.store.get(job.id)
        self.assertEqual(record.status, JobStatus.COMPLETED, record.error)
        self.assertEqual([url for url, _hint in calls], [first, second])

    def test_a_sign_in_wall_is_reported_as_one(self) -> None:
        # Otherwise this surfaced as "no engine resolved this URL", which reads
        # as a broken extractor rather than a page asking for an account.
        manager = self.discovering_manager()
        wall = Resolution(
            resolver="browser",
            page_url="https://page.example.test/watch/3",
            final_url="https://page.example.test/auth/login?currentUrl=%2Fwatch%2F3",
            title="Sign in",
            media=(),
            engine="browser",
        )

        with patch("videotrack.engines.chain.resolve", return_value=[wall]):
            job = manager.submit("https://page.example.test/watch/3")
            self.wait_for_status(job.id, JobStatus.FAILED)

        self.assertIn("requires signing in", self.store.get(job.id).error)

    def test_a_resolution_carrying_nothing_reports_that_plainly(self) -> None:
        manager = self.discovering_manager()
        empty = CaptureResult(
            page_url="https://page.example.test/watch/2",
            final_url="https://page.example.test/watch/2",
            title="Nothing Here",
            user_agent="test-agent",
            cookies={},
            requests=[],
        )
        resolution_id = manager.cache.put(self.browser_resolution(empty))

        job = manager.submit("https://page.example.test/watch/2", resolution_id=resolution_id)
        self.wait_for_status(job.id, JobStatus.FAILED)

        self.assertIn("no media", self.store.get(job.id).error)


if __name__ == "__main__":
    unittest.main()
