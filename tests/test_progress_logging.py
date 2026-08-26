"""Progress has to be visible while it happens.

The package already reported its stages as log records and as events, but nothing
configured a handler for the records and the events only reached a connected
client. An operator watching the console could not tell a multi-minute browser
capture from a hang, which is exactly the mistake that gets a working download
killed halfway through.
"""

from __future__ import annotations

import logging
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from videotrack.core.events import PROGRESS, PipelineEvent, Progress, progress_event
from videotrack.core.options import PipelineOptions
from videotrack.jobs.bus import EventBus
from videotrack.jobs.manager import PROGRESS_LOG_INTERVAL_SECONDS, JobManager
from videotrack.jobs.models import Job, JobStatus
from videotrack.jobs.store import JobStore
from videotrack.logs import DEFAULT_LEVEL, ENV_LOG_LEVEL, configure_logging, resolve_level

SETTLE_SECONDS = 3.0


class LevelResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = dict(__import__("os").environ)

        def restore() -> None:
            import os

            os.environ.clear()
            os.environ.update(self._saved)

        self.addCleanup(restore)

    def test_an_explicit_level_wins(self) -> None:
        self.assertEqual(resolve_level("debug"), "DEBUG")

    def test_the_environment_is_used_when_nothing_is_explicit(self) -> None:
        import os

        os.environ[ENV_LOG_LEVEL] = "warning"

        self.assertEqual(resolve_level(None), "WARNING")

    def test_an_explicit_level_beats_the_environment(self) -> None:
        import os

        os.environ[ENV_LOG_LEVEL] = "warning"

        self.assertEqual(resolve_level("DEBUG"), "DEBUG")

    def test_an_unrecognized_name_falls_back_rather_than_raising(self) -> None:
        # A typo in a config value must not stop the program from starting.
        self.assertEqual(resolve_level("chatty"), DEFAULT_LEVEL)

    def test_nothing_configured_is_info(self) -> None:
        self.assertEqual(resolve_level(None), DEFAULT_LEVEL)


class ConfigureLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logger = logging.getLogger("videotrack")
        saved = (list(self.logger.handlers), self.logger.level, self.logger.propagate)

        def restore() -> None:
            self.logger.handlers = saved[0]
            self.logger.setLevel(saved[1])
            self.logger.propagate = saved[2]

        self.addCleanup(restore)

    def test_records_reach_a_handler(self) -> None:
        configure_logging("INFO")

        with self.assertLogs("videotrack.core.capture", level="INFO") as captured:
            logging.getLogger("videotrack.core.capture").info("capture: page ready at 1.2s")

        self.assertIn("page ready", "\n".join(captured.output))

    def test_calling_twice_does_not_double_the_handlers(self) -> None:
        configure_logging("INFO")
        first = len(self.logger.handlers)
        configure_logging("INFO")

        self.assertEqual(len(self.logger.handlers), first)

    def test_only_the_package_logger_is_touched(self) -> None:
        # Reconfiguring the root logger would fight uvicorn, pytest, or whatever
        # else is hosting this process.
        root = logging.getLogger()
        before = (list(root.handlers), root.level)

        configure_logging("DEBUG")

        self.assertEqual(list(root.handlers), before[0])
        self.assertEqual(root.level, before[1])

    def test_the_level_is_applied(self) -> None:
        configure_logging("WARNING")

        self.assertEqual(self.logger.level, logging.WARNING)


class JobEventLoggingTests(unittest.TestCase):
    """Every event the client sees, the console sees too."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.store = JobStore(":memory:")
        self.addCleanup(self.store.close)
        self.bus = EventBus()

    def build(self, runner) -> JobManager:
        manager = JobManager(
            store=self.store,
            bus=self.bus,
            options=PipelineOptions(output_dir=Path(self._temp.name)),
            concurrency=1,
            runner=runner,
        )
        self.addCleanup(manager.shutdown)
        return manager

    def wait_for(self, predicate, timeout: float = SETTLE_SECONDS) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_a_stage_message_is_logged(self) -> None:
        def runner(job, resolution, cancel, on_event):
            on_event(PipelineEvent("info", {"message": "Deep scan 1 embed URL(s)."}))
            out = Path(job.output_path)
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        with self.assertLogs("videotrack.jobs.manager", level="INFO") as captured:
            job = manager.submit("https://page.example.test/w/1")
            self.assertTrue(
                self.wait_for(lambda: self.store.get(job.id).status == JobStatus.COMPLETED)
            )

        joined = "\n".join(captured.output)
        self.assertIn("Deep scan 1 embed URL(s).", joined)
        self.assertIn(job.id[:8], joined)

    def test_an_error_message_is_logged(self) -> None:
        def runner(job, resolution, cancel, on_event):
            raise RuntimeError("cdn refused this candidate")

        manager = self.build(runner)
        with self.assertLogs("videotrack.jobs.manager", level="INFO") as captured:
            job = manager.submit("https://page.example.test/w/2")
            self.assertTrue(
                self.wait_for(lambda: self.store.get(job.id).status == JobStatus.FAILED)
            )

        self.assertIn("cdn refused this candidate", "\n".join(captured.output))

    def emit_progress(self, count: int):
        def runner(job, resolution, cancel, on_event):
            for index in range(count):
                on_event(progress_event(Progress(phase="downloading", percent=float(index))))
            out = Path(job.output_path)
            out.write_bytes(b"x")
            return out

        return runner

    def progress_lines_for(self, interval: float, count: int, url: str) -> list[str]:
        """Drive `count` progress events with the throttle set to `interval`.

        The interval is what gets controlled rather than the clock. The manager
        reads `time.monotonic` from the shared module, so patching that would
        reach the waiting loop in this test as well.
        """
        manager = self.build(self.emit_progress(count))
        with patch("videotrack.jobs.manager.PROGRESS_LOG_INTERVAL_SECONDS", interval):
            with self.assertLogs("videotrack.jobs.manager", level="INFO") as captured:
                job = manager.submit(url)
                self.assertTrue(
                    self.wait_for(lambda: self.store.get(job.id).status == JobStatus.COMPLETED)
                )
        return [line for line in captured.output if "downloading" in line]

    def test_progress_is_throttled_rather_than_flooding(self) -> None:
        # FFmpeg reports several times a second. Unthrottled, those lines bury
        # the stage messages that say what the work is actually doing.
        lines = self.progress_lines_for(3600.0, 40, "https://page.example.test/w/3")

        self.assertEqual(len(lines), 1, lines)

    def test_every_report_is_logged_when_the_throttle_is_open(self) -> None:
        lines = self.progress_lines_for(0.0, 3, "https://page.example.test/w/4")

        self.assertEqual(len(lines), 3, lines)

    def test_an_unknown_percent_is_reported_as_unknown_not_zero(self) -> None:
        def runner(job, resolution, cancel, on_event):
            on_event(PipelineEvent(PROGRESS, {"phase": "downloading", "percent": None}))
            out = Path(job.output_path)
            out.write_bytes(b"x")
            return out

        manager = self.build(runner)
        with self.assertLogs("videotrack.jobs.manager", level="INFO") as captured:
            job = manager.submit("https://page.example.test/w/5")
            self.assertTrue(
                self.wait_for(lambda: self.store.get(job.id).status == JobStatus.COMPLETED)
            )

        self.assertIn("unknown", "\n".join(captured.output))


class ProgressThrottleClockTests(unittest.TestCase):
    """The throttle must not depend on how long the machine has been up.

    `_log_event` is driven directly here, with no worker thread, which is what
    makes it safe to control the clock: patching `time.monotonic` for a threaded
    test also reaches the waiting loop in the test itself.
    """

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.store = JobStore(":memory:")
        self.addCleanup(self.store.close)
        self.manager = JobManager(
            store=self.store,
            bus=EventBus(),
            options=PipelineOptions(output_dir=Path(self._temp.name)),
            concurrency=1,
            runner=lambda *args: Path(self._temp.name) / "unused.mp4",
        )
        self.addCleanup(self.manager.shutdown)
        self.job = Job(url="https://page.example.test/w/1")

    def log_at(self, seconds: float) -> list[str]:
        event = progress_event(Progress(phase="downloading", percent=1.0))
        with patch("videotrack.jobs.manager.time.monotonic", return_value=seconds):
            with self.assertLogs("videotrack.jobs.manager", level="INFO") as captured:
                self.manager._log_event(self.job, event)
                # A record of its own, so assertLogs never fails for emptiness
                # and the count below stays about progress alone.
                logging.getLogger("videotrack.jobs.manager").info("marker")
        return [line for line in captured.output if "downloading" in line]

    def test_the_first_report_is_logged_on_a_freshly_booted_machine(self) -> None:
        # Regression: the previous-timestamp default was zero, compared against a
        # monotonic clock. Below the throttle interval - a fresh boot, or a CI
        # runner - that arithmetic swallowed the first report of every job.
        self.assertEqual(len(self.log_at(1.0)), 1)

    def test_a_second_report_inside_the_interval_is_dropped(self) -> None:
        self.log_at(1.0)

        self.assertEqual(len(self.log_at(1.5)), 0)

    def test_a_report_after_the_interval_is_logged_again(self) -> None:
        self.log_at(1.0)

        self.assertEqual(len(self.log_at(1.0 + PROGRESS_LOG_INTERVAL_SECONDS + 0.1)), 1)

    def test_each_job_is_throttled_on_its_own(self) -> None:
        self.log_at(1.0)
        other = Job(url="https://page.example.test/w/2")
        event = progress_event(Progress(phase="downloading", percent=1.0))

        with patch("videotrack.jobs.manager.time.monotonic", return_value=1.5):
            with self.assertLogs("videotrack.jobs.manager", level="INFO") as captured:
                self.manager._log_event(other, event)

        self.assertEqual(len([line for line in captured.output if "downloading" in line]), 1)


if __name__ == "__main__":
    unittest.main()
