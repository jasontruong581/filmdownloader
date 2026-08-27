"""A failure has to say what went wrong.

Observed: a job failed with twenty lines describing a stream FFmpeg had read
perfectly - resolution, codecs, `Stream mapping: copy` - and no mention of any
fault, repeated three times across `failed`, `candidate_rejected` and
`job_failed`. The reason was not missing from FFmpeg's output. It was reported
while FFmpeg parsed the playlist, and the informational dump that followed
pushed it out of the trailing window the executor kept.

Two problems, both about reporting rather than downloading: the diagnosis was
searched for in the wrong place, and once found it was mirrored at every level
unbounded.
"""

from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from videotrack.core import download as download_module
from videotrack.core import ffmpeg_executor as executor_module
from videotrack.core.download import (
    FFMPEG_ERROR_REPORT_LINES,
    summarize_ffmpeg_error,
)
from videotrack.core.events import PipelineEvent
from videotrack.core.executor import DownloadRequest
from videotrack.core.ffmpeg_executor import FfmpegExecutor
from videotrack.core.models import CaptureResult, StreamCandidate
from videotrack.core.options import PipelineOptions
from videotrack.jobs.bus import EventBus
from videotrack.jobs.manager import LOGGED_MESSAGE_LIMIT, JobManager
from videotrack.jobs.store import JobStore

SETTLE_SECONDS = 3.0

#: The shape that produced the unreadable report: the fault first, then FFmpeg
#: describing what it did manage to read.
REFUSED_PLAYLIST_STDERR = [
    "[https @ 000001] Opening 'https://cdn.example.test/master.m3u8' for reading",
    "[hls @ 000001] Skip ('#EXT-X-VERSION:3')",
    "[hls @ 000001] mime type is not rfc8216 compliant",
    "[hls @ 000001] Opening 'https://cdn.example.test/seg1' for reading",
    "Input #0, hls, from 'https://cdn.example.test/master.m3u8':",
    "  Duration: 00:24:11.00, start: 0.111111, bitrate: N/A",
    "  Program 0",
    "    Metadata:",
    "      variant_bitrate : 0",
    "  Stream #0:0[0x0]: Video: h264 (High), yuv420p(tv, bt709), 1920x1080, 30 fps",
    "    Metadata:",
    "      variant_bitrate : 0",
    "  Stream #0:1[0x0]: Audio: aac (LC), 44100 Hz, stereo, fltp, 130 kb/s",
    "Stream mapping:",
    "  Stream #0:0 -> #0:0 (copy)",
    "  Stream #0:1 -> #0:1 (copy)",
]


def _capture() -> CaptureResult:
    return CaptureResult(
        page_url="https://page.example.test/watch",
        final_url="https://page.example.test/watch",
        title="Clip",
        user_agent="test-agent",
        cookies={},
        requests=[],
    )


def _candidate(url: str = "https://cdn.example.test/a.mp4", kind: str = "mp4") -> StreamCandidate:
    return StreamCandidate(url=url, kind=kind, score=10, source="main")


class ErrorSummaryTests(unittest.TestCase):
    def test_the_fault_is_found_behind_the_informational_dump(self) -> None:
        # The whole defect in one assertion: the reason is present and was being
        # discarded in favour of an accurate description of a successful read.
        summary = summarize_ffmpeg_error(REFUSED_PLAYLIST_STDERR, 3199971767)

        self.assertIn("mime type is not rfc8216 compliant", summary)

    def test_the_stream_dump_is_left_out(self) -> None:
        summary = summarize_ffmpeg_error(REFUSED_PLAYLIST_STDERR, 1)

        self.assertNotIn("Stream mapping", summary)
        self.assertNotIn("variant_bitrate", summary)
        self.assertNotIn("h264", summary)

    def test_the_report_is_bounded(self) -> None:
        many = [f"Error number {index} occurred" for index in range(40)]

        summary = summarize_ffmpeg_error(many, 1)

        self.assertEqual(len(summary.splitlines()), FFMPEG_ERROR_REPORT_LINES)

    def test_the_most_recent_faults_are_the_ones_kept(self) -> None:
        lines = ["Error first", *[f"benign line {i}" for i in range(20)], "Error last"]

        summary = summarize_ffmpeg_error(lines, 1)

        self.assertIn("Error last", summary)

    def test_silence_is_reported_as_the_finding(self) -> None:
        # Not papered over: if FFmpeg said nothing diagnostic then the exit code
        # is the only evidence there is, and it has to be shown.
        summary = summarize_ffmpeg_error(["Stream mapping:", "  Stream #0:0 -> #0:0 (copy)"], 8)

        self.assertIn("no error text", summary)
        self.assertIn("8", summary)

    def test_no_output_at_all_is_distinguished_from_no_error(self) -> None:
        summary = summarize_ffmpeg_error([], 8)

        self.assertIn("no output at all", summary)
        self.assertIn("8", summary)

    def test_a_stall_note_counts_as_the_diagnosis(self) -> None:
        # The executor writes this itself, in the one case where FFmpeg is
        # guaranteed to have said nothing.
        lines = [*REFUSED_PLAYLIST_STDERR[4:], "stalled: no output for 120s"]

        summary = summarize_ffmpeg_error(lines, 1)

        self.assertIn("stalled", summary)

    def test_blank_lines_do_not_fill_the_report(self) -> None:
        summary = summarize_ffmpeg_error(["", "  ", "Invalid data found", "", " "], 1)

        self.assertEqual(summary, "Invalid data found")

    def test_a_description_of_a_working_stream_is_not_a_fault(self) -> None:
        for line in REFUSED_PLAYLIST_STDERR[4:]:
            with self.subTest(line=line):
                self.assertFalse(download_module._is_fault_line(line))


class _RefusingProcess:
    """FFmpeg that reported the fault, then described what it read, then failed."""

    def __init__(self, *args, **kwargs) -> None:
        self.stdout = iter(())
        self.stderr = iter(f"{line}\n" for line in REFUSED_PLAYLIST_STDERR)
        self.returncode = 1

    def wait(self, timeout: float | None = None) -> int:
        return 1

    def poll(self) -> int:
        return 1

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class ExecutorReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.out_file = Path(self._temp.name) / "Clip.mp4"

        for name in ("hls_strictness_flags", "network_resilience_flags"):
            flags_patch = patch.object(download_module, name, return_value=())
            flags_patch.start()
            self.addCleanup(flags_patch.stop)

    def _request(self, candidate: StreamCandidate) -> DownloadRequest:
        return DownloadRequest(out_file=self.out_file, capture=_capture(), candidate=candidate)

    def test_a_direct_failure_reports_the_fault(self) -> None:
        with patch.object(executor_module.subprocess, "Popen", _RefusingProcess):
            with self.assertRaises(RuntimeError) as caught:
                FfmpegExecutor().run(
                    self._request(_candidate()), threading.Event(), lambda event: None
                )

        self.assertIn("mime type is not rfc8216 compliant", str(caught.exception))

    def test_a_failed_repack_names_the_exit_code(self) -> None:
        # It was dropped here entirely, which is why this path could say nothing
        # about why the direct attempt had been abandoned.
        def failing_repack(*args, **kwargs):
            raise RuntimeError("segment request failed at 3/412: HTTP 403")

        with patch.object(executor_module.subprocess, "Popen", _RefusingProcess), patch.object(
            executor_module, "download_obfuscated_hls", side_effect=failing_repack
        ):
            with self.assertRaises(RuntimeError) as caught:
                FfmpegExecutor().run(
                    self._request(_candidate("https://cdn.example.test/a.m3u8", "hls")),
                    threading.Event(),
                    lambda event: None,
                )

        message = str(caught.exception)
        self.assertIn("exited 1", message)
        self.assertIn("HTTP 403", message)

    def test_a_failed_repack_leads_with_what_ended_the_attempt(self) -> None:
        def failing_repack(*args, **kwargs):
            raise RuntimeError("segment request failed at 3/412: HTTP 403")

        with patch.object(executor_module.subprocess, "Popen", _RefusingProcess), patch.object(
            executor_module, "download_obfuscated_hls", side_effect=failing_repack
        ):
            with self.assertRaises(RuntimeError) as caught:
                FfmpegExecutor().run(
                    self._request(_candidate("https://cdn.example.test/a.m3u8", "hls")),
                    threading.Event(),
                    lambda event: None,
                )

        self.assertTrue(str(caught.exception).startswith("the repack failed"))


class LoggedMessageTests(unittest.TestCase):
    """The log is for scanning; the job record is for reading."""

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

    def test_a_short_message_is_logged_unchanged(self) -> None:
        from videotrack.jobs.manager import _for_log

        self.assertEqual(_for_log("mime type is not rfc8216 compliant"), "mime type is not rfc8216 compliant")

    def test_newlines_are_collapsed_onto_one_line(self) -> None:
        from videotrack.jobs.manager import _for_log

        # A multi-line record breaks the shape the rest of the log has.
        self.assertNotIn("\n", _for_log("first\nsecond\nthird"))

    def test_a_long_message_states_what_it_withheld(self) -> None:
        from videotrack.jobs.manager import _for_log

        logged = _for_log("x" * (LOGGED_MESSAGE_LIMIT + 500))

        self.assertIn("more chars", logged)
        self.assertIn("full text on the job", logged)

    def test_the_logged_line_is_bounded(self) -> None:
        from videotrack.jobs.manager import _for_log

        logged = _for_log("y" * 5000)

        self.assertLess(len(logged), LOGGED_MESSAGE_LIMIT + 100)

    def test_the_job_keeps_the_whole_message_the_log_trimmed(self) -> None:
        # The invariant that makes trimming safe. The UI reads the job record.
        long_error = "Invalid data found. " + "detail " * 200

        def runner(job, resolution, cancel, on_event):
            on_event(PipelineEvent("failed", {"reason": "ffmpeg_failed", "error": long_error}))
            raise RuntimeError(long_error)

        manager = self.build(runner)
        with self.assertLogs("videotrack.jobs.manager", level="INFO") as captured:
            job = manager.submit("https://page.example.test/w/1")
            self.assertTrue(self.wait_for(lambda: self.store.get(job.id).status.value == "failed"))

        stored = self.store.get(job.id)
        self.assertIn("detail " * 200, stored.error)

        failure_lines = [line for line in captured.output if "failed" in line]
        self.assertTrue(failure_lines)
        for line in failure_lines:
            self.assertLess(len(line), LOGGED_MESSAGE_LIMIT + 200)


if __name__ == "__main__":
    unittest.main()
