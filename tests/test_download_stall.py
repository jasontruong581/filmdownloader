"""A transfer that stops delivering data must not become a job that never ends.

Observed: FFmpeg held a socket the far end had already closed, with two
connections in CLOSE_WAIT, the output file frozen at 53 MB, and the process
still alive seven hours later. Nothing was wrong with the command it was given.
Two separate gaps kept it there, and either one alone is enough to hang:

  * every FFmpeg reconnect option ships off, and the HTTP protocol offers no
    read timeout, so a dropped connection is not something FFmpeg notices; and
  * the executor read FFmpeg's pipe directly, so its blocked read became the
    job's blocked read - and the cancel flag, checked only once a line arrived,
    was unreachable for as long as FFmpeg said nothing.

No FFmpeg and no network here: the capability probe and the process are stubbed.
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
from videotrack.core.download import build_ffmpeg_command, network_resilience_flags
from videotrack.core.events import DOWNLOAD_COMPLETED, PipelineEvent
from videotrack.core.executor import DownloadCancelled, DownloadRequest
from videotrack.core.ffmpeg_executor import FfmpegExecutor
from videotrack.core.models import CaptureResult, StreamCandidate

OUT = Path("out.mp4")

#: Trimmed from the real `ffmpeg -h protocol=http` output.
HTTP_HELP = """
 -reconnect          <boolean>    .D......... auto reconnect after disconnect before EOF
 -reconnect_streamed <boolean>    .D......... auto reconnect streamed / non seekable streams
 -reconnect_delay_max <int>       .D......... max reconnect delay in seconds
 -reconnect_on_network_error <boolean> .D... auto reconnect in case of tcp/tls error
 -reconnect_on_http_error <string> .D...... list of http status codes to reconnect on
"""


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


class _Help:
    """Stands in for a completed `subprocess.run` carrying help text."""

    def __init__(self, text: str) -> None:
        self.stdout = text
        self.stderr = ""


class NetworkResilienceFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        network_resilience_flags.cache_clear()
        self.addCleanup(network_resilience_flags.cache_clear)

    def test_every_option_this_build_offers_is_passed(self) -> None:
        with patch.object(download_module.subprocess, "run", return_value=_Help(HTTP_HELP)):
            flags = network_resilience_flags(None)

        self.assertEqual(flags[flags.index("-reconnect") + 1], "1")
        self.assertEqual(flags[flags.index("-reconnect_streamed") + 1], "1")
        self.assertEqual(flags[flags.index("-reconnect_on_network_error") + 1], "1")

    def test_an_option_this_build_lacks_is_left_out(self) -> None:
        # Passing an option an older FFmpeg does not have makes it exit before
        # downloading anything, which is why this is probed rather than assumed.
        without = HTTP_HELP.replace("-reconnect_on_network_error", "-something_else")
        with patch.object(download_module.subprocess, "run", return_value=_Help(without)):
            flags = network_resilience_flags(None)

        self.assertNotIn("-reconnect_on_network_error", flags)
        self.assertIn("-reconnect", flags)

    def test_a_probe_that_cannot_run_yields_no_flags(self) -> None:
        with patch.object(download_module.subprocess, "run", side_effect=OSError("no binary")):
            self.assertEqual(network_resilience_flags("nowhere"), ())

    def test_the_retry_delay_is_bounded_below_the_ffmpeg_default(self) -> None:
        # FFmpeg gives up after 120s by default, which is two minutes spent on a
        # stream that is already gone.
        with patch.object(download_module.subprocess, "run", return_value=_Help(HTTP_HELP)):
            flags = network_resilience_flags(None)

        delay = int(flags[flags.index("-reconnect_delay_max") + 1])
        self.assertGreater(delay, 0)
        self.assertLess(delay, 120)

    def test_only_server_faults_are_retried(self) -> None:
        # A 4xx is a verdict, not a hiccup: an expired token answers 403 for as
        # long as anyone asks, and retrying it would rebuild the hang.
        with patch.object(download_module.subprocess, "run", return_value=_Help(HTTP_HELP)):
            flags = network_resilience_flags(None)

        codes = flags[flags.index("-reconnect_on_http_error") + 1]
        self.assertIn("5xx", codes)
        self.assertNotIn("4xx", codes)

    def test_the_probe_asks_the_binary_the_command_will_run(self) -> None:
        with patch.object(download_module, "resolve_tool", return_value=r"C:\ffmpeg\bin\ffmpeg.exe"):
            with patch.object(
                download_module.subprocess, "run", return_value=_Help(HTTP_HELP)
            ) as run:
                network_resilience_flags(r"C:\ffmpeg\bin")

        self.assertEqual(run.call_args.args[0][0], r"C:\ffmpeg\bin\ffmpeg.exe")


class CommandWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in ("hls_strictness_flags", "network_resilience_flags"):
            flags_patch = patch.object(
                download_module, name, return_value=("-reconnect", "1") if "network" in name else ()
            )
            flags_patch.start()
            self.addCleanup(flags_patch.stop)

    def test_an_http_input_gets_the_flags(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate(), OUT)

        self.assertIn("-reconnect", cmd)

    def test_the_flags_precede_the_input(self) -> None:
        # They are input options; after `-i` FFmpeg reads them as output options.
        cmd = build_ffmpeg_command(_capture(), _candidate(), OUT)

        self.assertLess(cmd.index("-reconnect"), cmd.index("-i"))

    def test_a_local_input_gets_none(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate(url="file:///tmp/a.mp4"), OUT)

        self.assertNotIn("-reconnect", cmd)

    def test_a_plain_file_over_http_gets_them_too(self) -> None:
        # Not restricted to playlists: a single MP4 stalls the same way, and has
        # the same nothing to fall back on.
        cmd = build_ffmpeg_command(_capture(), _candidate(kind="mp4"), OUT)

        self.assertIn("-reconnect", cmd)


class _SilentProcess:
    """FFmpeg running, connected, and producing nothing - the observed hang."""

    instances: list["_SilentProcess"] = []

    def __init__(self, cmd, *args, **kwargs) -> None:
        self.cmd = cmd
        self.released = threading.Event()
        self.terminated = False
        self.stdout = self
        self.stderr = iter(())
        self.returncode = None
        type(self).instances.append(self)

    def __iter__(self) -> "_SilentProcess":
        return self

    def __next__(self) -> str:
        # Blocks exactly as a read on a socket the far end abandoned does.
        self.released.wait(timeout=30)
        raise StopIteration

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 1
        return 1

    def poll(self) -> int | None:
        return 1 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True
        self.released.set()

    def kill(self) -> None:
        self.terminate()


class _TalkingProcess:
    """FFmpeg reporting steadily, then finishing - a transfer that works."""

    instances: list["_TalkingProcess"] = []

    def __init__(self, cmd, *args, **kwargs) -> None:
        self.cmd = cmd
        self.terminated = False
        self.returncode = 0
        # The part file is the last argument, and writing it is what a real run
        # does; the executor refuses an empty output.
        Path(cmd[-1]).write_bytes(b"media")
        self.stdout = self._lines()
        self.stderr = iter(())
        type(self).instances.append(self)

    def _lines(self):
        for index in range(6):
            time.sleep(0.02)
            yield f"out_time_ms={index * 1000}\n"
            yield "progress=continue\n"

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int:
        return 0

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminate()


class StallWatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.out_file = Path(self._temp.name) / "Clip.mp4"
        self.events: list[PipelineEvent] = []

        _SilentProcess.instances = []
        _TalkingProcess.instances = []

        for name in ("hls_strictness_flags", "network_resilience_flags"):
            flags_patch = patch.object(download_module, name, return_value=())
            flags_patch.start()
            self.addCleanup(flags_patch.stop)

        # The watchdog is driven by these two constants rather than by a patched
        # clock: the executor reads the clock from the shared `time` module, and
        # patching it there reaches the test's own waiting as well.
        for name, value in (("STALL_TIMEOUT_SECONDS", 0.3), ("CANCEL_POLL_SECONDS", 0.02)):
            constant = patch.object(executor_module, name, value)
            constant.start()
            self.addCleanup(constant.stop)

    def _request(self, candidate: StreamCandidate) -> DownloadRequest:
        return DownloadRequest(out_file=self.out_file, capture=_capture(), candidate=candidate)

    def _record(self, event: PipelineEvent) -> None:
        self.events.append(event)

    def test_a_silent_ffmpeg_is_abandoned_rather_than_waited_on(self) -> None:
        with patch.object(executor_module.subprocess, "Popen", _SilentProcess):
            with self.assertRaises(RuntimeError) as caught:
                FfmpegExecutor().run(
                    self._request(_candidate()), threading.Event(), self._record
                )

        self.assertIn("stalled", str(caught.exception))

    def test_the_stalled_process_is_stopped(self) -> None:
        # Leaving it running is how sixty-five orphaned processes accumulate.
        with patch.object(executor_module.subprocess, "Popen", _SilentProcess):
            with self.assertRaises(RuntimeError):
                FfmpegExecutor().run(
                    self._request(_candidate()), threading.Event(), self._record
                )

        self.assertTrue(_SilentProcess.instances[0].terminated)

    def test_the_message_says_how_long_the_silence_lasted(self) -> None:
        with patch.object(executor_module.subprocess, "Popen", _SilentProcess):
            with self.assertRaises(RuntimeError) as caught:
                FfmpegExecutor().run(
                    self._request(_candidate()), threading.Event(), self._record
                )

        # FFmpeg reports nothing about this itself, so the executor has to.
        self.assertIn("no output for", str(caught.exception))

    def test_cancel_takes_effect_while_ffmpeg_is_silent(self) -> None:
        # The regression: the cancel check sat inside the loop over stdout, so a
        # silent FFmpeg meant it was never reached and Cancel did nothing.
        cancel = threading.Event()
        cancel.set()

        with patch.object(executor_module.subprocess, "Popen", _SilentProcess):
            with self.assertRaises(DownloadCancelled):
                FfmpegExecutor().run(self._request(_candidate()), cancel, self._record)

    def test_cancel_does_not_wait_out_the_stall_timeout(self) -> None:
        # With the stall timeout long, only the cancel check in the idle branch
        # can end this promptly. The margin is wide on purpose: the point is the
        # difference between seconds and half a minute, not a precise duration.
        cancel = threading.Event()
        cancel.set()

        with patch.object(executor_module, "STALL_TIMEOUT_SECONDS", 30.0), patch.object(
            executor_module.subprocess, "Popen", _SilentProcess
        ):
            started = time.monotonic()
            with self.assertRaises(DownloadCancelled):
                FfmpegExecutor().run(self._request(_candidate()), cancel, self._record)
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5.0)

    def test_cancellation_is_not_reported_as_a_stall(self) -> None:
        cancel = threading.Event()
        cancel.set()

        with patch.object(executor_module.subprocess, "Popen", _SilentProcess):
            with self.assertRaises(DownloadCancelled) as caught:
                FfmpegExecutor().run(self._request(_candidate()), cancel, self._record)

        self.assertNotIn("stalled", str(caught.exception))

    def test_a_reporting_ffmpeg_is_left_alone(self) -> None:
        with patch.object(executor_module.subprocess, "Popen", _TalkingProcess):
            result = FfmpegExecutor().run(
                self._request(_candidate()), threading.Event(), self._record
            )

        self.assertEqual(result, self.out_file)
        self.assertFalse(_TalkingProcess.instances[0].terminated)
        self.assertIn(DOWNLOAD_COMPLETED, [event.kind for event in self.events])

    def test_a_finished_ffmpeg_does_not_wait_out_the_timeout(self) -> None:
        # Closed stdout has to end the loop on its own; without that every
        # successful download would pause for the full stall timeout.
        started = time.monotonic()
        with patch.object(executor_module.subprocess, "Popen", _TalkingProcess):
            FfmpegExecutor().run(self._request(_candidate()), threading.Event(), self._record)

        self.assertLess(time.monotonic() - started, executor_module.STALL_TIMEOUT_SECONDS)

    def test_a_stalled_playlist_still_reaches_the_repack(self) -> None:
        # A stall is a failed attempt, not a dead candidate: the segment fetcher
        # has its own per-request timeouts and may well succeed.
        def fake_repack(capture, candidate, out_file, cancel=None, on_progress=None):
            out_file.write_bytes(b"repacked")
            return out_file

        with patch.object(executor_module.subprocess, "Popen", _SilentProcess), patch.object(
            executor_module, "download_obfuscated_hls", side_effect=fake_repack
        ) as repack:
            result = FfmpegExecutor().run(
                self._request(_candidate("https://cdn.example.test/a.m3u8", "hls")),
                threading.Event(),
                self._record,
            )

        self.assertEqual(repack.call_count, 1)
        self.assertEqual(result.read_bytes(), b"repacked")


if __name__ == "__main__":
    unittest.main()
