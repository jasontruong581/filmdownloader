"""Job persistence.

The property that matters most: a job that was running when the process died is
never reported as complete.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from videotrack.jobs.models import ACTIVE_STATUSES, Batch, Job, JobStatus
from videotrack.jobs.store import SCHEMA_VERSION, JobStore


class SchemaTests(unittest.TestCase):
    def test_a_fresh_database_records_its_schema_version(self) -> None:
        store = JobStore(":memory:")
        self.addCleanup(store.close)

        self.assertEqual(store.schema_version, SCHEMA_VERSION)

    def test_reopening_a_database_does_not_duplicate_the_version_row(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "jobs.db"
            first = JobStore(path)
            first.close()

            second = JobStore(path)
            try:
                self.assertEqual(second.schema_version, SCHEMA_VERSION)
            finally:
                # Windows will not remove a directory holding an open handle.
                second.close()

    def test_the_parent_directory_is_created(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "nested" / "state" / "jobs.db"

            store = JobStore(path)
            try:
                self.assertTrue(path.parent.is_dir())
            finally:
                store.close()


class RoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(":memory:")
        self.addCleanup(self.store.close)

    def test_a_job_round_trips_through_the_database(self) -> None:
        job = self.store.add(
            Job(url="https://page.example.test/w/1", title="Clip", format_id="137", engine="ytdlp")
        )

        loaded = self.store.get(job.id)

        self.assertEqual(loaded.url, "https://page.example.test/w/1")
        self.assertEqual(loaded.title, "Clip")
        self.assertEqual(loaded.format_id, "137")
        self.assertEqual(loaded.engine, "ytdlp")
        self.assertEqual(loaded.status, JobStatus.QUEUED)

    def test_timestamps_are_set_on_insert(self) -> None:
        job = self.store.add(Job(url="https://page.example.test/w/1"))

        self.assertTrue(job.created_at)
        self.assertTrue(job.updated_at)

    def test_optional_measurements_survive_as_none(self) -> None:
        # A missing percent must stay missing; zero would read as a stall.
        job = self.store.add(Job(url="https://page.example.test/w/1"))

        loaded = self.store.get(job.id)

        self.assertIsNone(loaded.percent)
        self.assertIsNone(loaded.total_bytes)
        self.assertIsNone(loaded.error)

    def test_an_update_is_persisted(self) -> None:
        job = self.store.add(Job(url="https://page.example.test/w/1"))
        job.status = JobStatus.COMPLETED
        job.percent = 100.0
        job.output_path = "output/clip.mp4"

        self.store.update(job)

        loaded = self.store.get(job.id)
        self.assertEqual(loaded.status, JobStatus.COMPLETED)
        self.assertEqual(loaded.percent, 100.0)
        self.assertEqual(loaded.output_path, "output/clip.mp4")

    def test_an_unknown_id_returns_none(self) -> None:
        self.assertIsNone(self.store.get("does-not-exist"))


class ListingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(":memory:")
        self.addCleanup(self.store.close)

    def test_jobs_are_listed_in_creation_order(self) -> None:
        first = self.store.add(Job(url="https://page.example.test/w/1"))
        second = self.store.add(Job(url="https://page.example.test/w/2"))

        self.assertEqual([job.id for job in self.store.list()], [first.id, second.id])

    def test_listing_can_filter_by_status(self) -> None:
        done = self.store.add(Job(url="https://page.example.test/w/1"))
        done.status = JobStatus.COMPLETED
        self.store.update(done)
        self.store.add(Job(url="https://page.example.test/w/2"))

        self.assertEqual(len(self.store.list(status=JobStatus.COMPLETED)), 1)
        self.assertEqual(len(self.store.list(status="queued")), 1)

    def test_listing_can_filter_by_batch(self) -> None:
        batch = self.store.add_batch(Batch(source_url="https://page.example.test/list"))
        self.store.add(Job(url="https://page.example.test/w/1", batch_id=batch.id))
        self.store.add(Job(url="https://page.example.test/w/2"))

        self.assertEqual(len(self.store.list(batch_id=batch.id)), 1)

    def test_counts_are_grouped_by_status(self) -> None:
        self.store.add(Job(url="https://page.example.test/w/1"))
        self.store.add(Job(url="https://page.example.test/w/2"))

        self.assertEqual(self.store.counts_by_status(), {"queued": 2})


class DuplicateGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = JobStore(":memory:")
        self.addCleanup(self.store.close)

    def test_an_active_job_for_the_same_url_and_format_is_found(self) -> None:
        self.store.add(Job(url="https://page.example.test/w/1", format_id="137"))

        found = self.store.active_for("https://page.example.test/w/1", "137")

        self.assertIsNotNone(found)

    def test_a_different_format_is_not_a_duplicate(self) -> None:
        self.store.add(Job(url="https://page.example.test/w/1", format_id="137"))

        self.assertIsNone(self.store.active_for("https://page.example.test/w/1", "22"))

    def test_a_null_format_is_matched_as_null(self) -> None:
        self.store.add(Job(url="https://page.example.test/w/1", format_id=None))

        self.assertIsNotNone(self.store.active_for("https://page.example.test/w/1", None))
        self.assertIsNone(self.store.active_for("https://page.example.test/w/1", "137"))

    def test_a_finished_job_no_longer_blocks_a_resubmission(self) -> None:
        job = self.store.add(Job(url="https://page.example.test/w/1"))
        job.status = JobStatus.COMPLETED
        self.store.update(job)

        self.assertIsNone(self.store.active_for("https://page.example.test/w/1", None))


class RecoveryTests(unittest.TestCase):
    def test_a_job_left_running_is_recovered_as_interrupted(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "jobs.db"
            first = JobStore(path)
            job = first.add(Job(url="https://page.example.test/w/1"))
            job.status = JobStatus.DOWNLOADING
            job.percent = 42.0
            first.update(job)
            first.close()

            # Reopening stands in for the process having died mid-download.
            second = JobStore(path)
            try:
                recovered = second.recover_interrupted()

                self.assertEqual([r.status for r in recovered], [JobStatus.INTERRUPTED])
                self.assertEqual(second.get(job.id).status, JobStatus.INTERRUPTED)
            finally:
                second.close()

    def test_an_interrupted_job_is_never_reported_as_complete(self) -> None:
        store = JobStore(":memory:")
        self.addCleanup(store.close)
        job = store.add(Job(url="https://page.example.test/w/1"))
        job.status = JobStatus.DOWNLOADING
        store.update(job)

        store.recover_interrupted()

        self.assertNotEqual(store.get(job.id).status, JobStatus.COMPLETED)

    def test_every_active_status_is_recovered(self) -> None:
        store = JobStore(":memory:")
        self.addCleanup(store.close)
        for status in sorted(ACTIVE_STATUSES, key=lambda s: s.value):
            job = store.add(Job(url=f"https://page.example.test/{status.value}"))
            job.status = status
            store.update(job)

        recovered = store.recover_interrupted()

        self.assertEqual(len(recovered), len(ACTIVE_STATUSES))

    def test_finished_jobs_are_left_alone(self) -> None:
        store = JobStore(":memory:")
        self.addCleanup(store.close)
        job = store.add(Job(url="https://page.example.test/w/1"))
        job.status = JobStatus.COMPLETED
        store.update(job)

        self.assertEqual(store.recover_interrupted(), [])
        self.assertEqual(store.get(job.id).status, JobStatus.COMPLETED)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_writes_do_not_raise_database_is_locked(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = JobStore(Path(temp_dir) / "jobs.db")
            self.addCleanup(store.close)
            jobs = [store.add(Job(url=f"https://page.example.test/w/{i}")) for i in range(8)]
            errors: list[Exception] = []

            def hammer(job: Job) -> None:
                try:
                    for percent in range(0, 100, 10):
                        job.percent = float(percent)
                        store.update(job)
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

            threads = [threading.Thread(target=hammer, args=(job,)) for job in jobs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            store.close()


class BatchTests(unittest.TestCase):
    def test_a_batch_round_trips(self) -> None:
        store = JobStore(":memory:")
        self.addCleanup(store.close)

        batch = store.add_batch(
            Batch(source_url="https://page.example.test/list", capability="playlist", confidence="proven")
        )

        loaded = store.get_batch(batch.id)
        self.assertEqual(loaded.capability, "playlist")
        self.assertEqual(loaded.confidence, "proven")
        self.assertTrue(loaded.created_at)

    def test_an_unknown_batch_returns_none(self) -> None:
        store = JobStore(":memory:")
        self.addCleanup(store.close)

        self.assertIsNone(store.get_batch("nope"))


if __name__ == "__main__":
    unittest.main()
