"""What happens when FFmpeg refuses a playlist it should be able to read.

FFmpeg declines plenty of real streams: segments served with no usable
extension, a playlist whose MIME type is not RFC 8216 compliant, or a payload
wrapped behind another format's header. Fetching the segments directly is the
only thing that reads those, so the executor has to reach that fallback rather
than reporting the attempt as lost.

No FFmpeg, no network: the process and the transport are both stubbed.
"""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from videotrack.core import download as download_module
from videotrack.core.download import build_ffmpeg_command, is_hls_candidate
from videotrack.core.events import DOWNLOAD_COMPLETED, FAILED, INFO, PipelineEvent
from videotrack.core.executor import DownloadCancelled, DownloadRequest
from videotrack.core.ffmpeg_executor import FfmpegExecutor
from videotrack.core.models import CaptureResult, NetworkRequest, StreamCandidate


def _capture() -> CaptureResult:
    return CaptureResult(
        page_url="https://page.example.test/watch",
        final_url="https://page.example.test/watch",
        title="Clip",
        user_agent="test-agent",
        cookies={},
        requests=[
            NetworkRequest(
                url="https://cdn.example.test/hls/master.m3u8",
                method="GET",
                headers={},
                resource_type="Media",
                status=200,
            )
        ],
    )


def _candidate(url: str, kind: str) -> StreamCandidate:
    return StreamCandidate(url=url, kind=kind, score=10, source="main")


class _FailingProcess:
    """A process that opened nothing and exited non-zero, as FFmpeg does here."""

    def __init__(self, *args, **kwargs) -> None:
        self.stdout = iter(())
        self.stderr = iter(("mime type is not rfc8216 compliant\n",))
        self.returncode = 1

    def wait(self, timeout: float | None = None) -> int:
        return 1

    def poll(self) -> int:
        return 1

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


class HlsCandidateTests(unittest.TestCase):
    """One shared judgement, so the builder and the fallback cannot disagree."""

    def test_kind_decides_first(self) -> None:
        self.assertTrue(is_hls_candidate(_candidate("https://cdn.example.test/x", "hls")))
        self.assertTrue(is_hls_candidate(_candidate("https://cdn.example.test/x", "playlist")))

    def test_url_shape_decides_when_the_kind_does_not(self) -> None:
        self.assertTrue(is_hls_candidate(_candidate("https://cdn.example.test/a.m3u8", "media")))
        self.assertTrue(
            is_hls_candidate(_candidate("https://cdn.example.test/manifest-s1/03210.vl", "media"))
        )

    def test_a_plain_file_is_not_a_playlist(self) -> None:
        self.assertFalse(is_hls_candidate(_candidate("https://cdn.example.test/a.mp4", "mp4")))


class StrictnessFlagTests(unittest.TestCase):
    """FFmpeg 7.1 added a picky mode that allowed_extensions does not loosen.

    Probed rather than assumed, because passing an option an older build lacks
    makes it exit before downloading anything. Patched here so the built command
    does not depend on which FFmpeg the machine happens to have.
    """

    def test_the_flag_is_passed_when_the_build_supports_it(self) -> None:
        with patch.object(
            download_module, "hls_strictness_flags", return_value=("-extension_picky", "0")
        ):
            cmd = build_ffmpeg_command(
                _capture(), _candidate("https://cdn.example.test/a.m3u8", "hls"), Path("out.mp4")
            )

        self.assertIn("-extension_picky", cmd)
        self.assertEqual(cmd[cmd.index("-extension_picky") + 1], "0")
        # After the broad allowance, and still before the input.
        self.assertLess(cmd.index("-allowed_extensions"), cmd.index("-extension_picky"))
        self.assertLess(cmd.index("-extension_picky"), cmd.index("-i"))

    def test_the_flag_is_omitted_when_the_build_lacks_it(self) -> None:
        with patch.object(download_module, "hls_strictness_flags", return_value=()):
            cmd = build_ffmpeg_command(
                _capture(), _candidate("https://cdn.example.test/a.m3u8", "hls"), Path("out.mp4")
            )

        self.assertNotIn("-extension_picky", cmd)
        self.assertIn("-allowed_extensions", cmd)

    def test_a_plain_file_never_gets_playlist_flags(self) -> None:
        with patch.object(
            download_module, "hls_strictness_flags", return_value=("-extension_picky", "0")
        ):
            cmd = build_ffmpeg_command(
                _capture(), _candidate("https://cdn.example.test/a.mp4", "mp4"), Path("out.mp4")
            )

        self.assertNotIn("-extension_picky", cmd)
        self.assertNotIn("-allowed_extensions", cmd)


class ExecutorFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.out_file = Path(self._temp.name) / "Clip.mp4"
        self.events: list[PipelineEvent] = []

        # Both capability probes shell out, and these tests replace Popen, which
        # subprocess.run uses as a context manager. Pinning them keeps the built
        # command independent of whichever FFmpeg the machine has anyway.
        for name in ("hls_strictness_flags", "network_resilience_flags"):
            flags_patch = patch.object(download_module, name, return_value=())
            flags_patch.start()
            self.addCleanup(flags_patch.stop)

    def _request(self, candidate: StreamCandidate) -> DownloadRequest:
        return DownloadRequest(out_file=self.out_file, capture=_capture(), candidate=candidate)

    def _record(self, event: PipelineEvent) -> None:
        self.events.append(event)

    def _kinds(self) -> list[str]:
        return [event.kind for event in self.events]

    def test_a_refused_playlist_falls_through_to_the_repack(self) -> None:
        # Regression: this raised instead, so a stream only the repack can read
        # was reported as a dead candidate.
        def fake_repack(capture, candidate, out_file, cancel=None, on_progress=None):
            if on_progress is not None:
                on_progress(1, 2)
                on_progress(2, 2)
            out_file.write_bytes(b"repacked")
            return out_file

        with patch("videotrack.core.ffmpeg_executor.subprocess.Popen", _FailingProcess), patch(
            "videotrack.core.ffmpeg_executor.download_obfuscated_hls", side_effect=fake_repack
        ) as repack:
            result = FfmpegExecutor().run(
                self._request(_candidate("https://cdn.example.test/a.m3u8", "hls")),
                threading.Event(),
                self._record,
            )

        self.assertEqual(result, self.out_file)
        self.assertTrue(self.out_file.exists())
        repack.assert_called_once()
        self.assertIn(INFO, self._kinds())
        self.assertEqual(self._kinds()[-1], DOWNLOAD_COMPLETED)

    def test_the_repack_receives_the_cancel_event(self) -> None:
        seen: dict = {}

        def fake_repack(capture, candidate, out_file, cancel=None, on_progress=None):
            seen["cancel"] = cancel
            out_file.write_bytes(b"repacked")
            return out_file

        cancel = threading.Event()
        with patch("videotrack.core.ffmpeg_executor.subprocess.Popen", _FailingProcess), patch(
            "videotrack.core.ffmpeg_executor.download_obfuscated_hls", side_effect=fake_repack
        ):
            FfmpegExecutor().run(
                self._request(_candidate("https://cdn.example.test/a.m3u8", "hls")),
                cancel,
                self._record,
            )

        self.assertIs(seen["cancel"], cancel)

    def test_a_refused_plain_file_is_not_repacked(self) -> None:
        # The fallback reads playlists. A single file that FFmpeg refused has
        # nothing to enumerate, so the failure stands.
        with patch("videotrack.core.ffmpeg_executor.subprocess.Popen", _FailingProcess), patch(
            "videotrack.core.ffmpeg_executor.download_obfuscated_hls"
        ) as repack:
            with self.assertRaises(RuntimeError) as caught:
                FfmpegExecutor().run(
                    self._request(_candidate("https://cdn.example.test/a.mp4", "mp4")),
                    threading.Event(),
                    self._record,
                )

        repack.assert_not_called()
        self.assertIn("exit code 1", str(caught.exception))
        self.assertIn(FAILED, self._kinds())

    def test_a_failed_repack_reports_both_failures(self) -> None:
        with patch("videotrack.core.ffmpeg_executor.subprocess.Popen", _FailingProcess), patch(
            "videotrack.core.ffmpeg_executor.download_obfuscated_hls",
            side_effect=RuntimeError("manifest contains no segments"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                FfmpegExecutor().run(
                    self._request(_candidate("https://cdn.example.test/a.m3u8", "hls")),
                    threading.Event(),
                    self._record,
                )

        message = str(caught.exception)
        self.assertIn("rfc8216", message)
        self.assertIn("manifest contains no segments", message)

    def test_a_cancelled_repack_stays_cancelled(self) -> None:
        with patch("videotrack.core.ffmpeg_executor.subprocess.Popen", _FailingProcess), patch(
            "videotrack.core.ffmpeg_executor.download_obfuscated_hls",
            side_effect=DownloadCancelled("cancelled while repacking segments"),
        ):
            with self.assertRaises(DownloadCancelled):
                FfmpegExecutor().run(
                    self._request(_candidate("https://cdn.example.test/a.m3u8", "hls")),
                    threading.Event(),
                    self._record,
                )

    def test_a_plugin_that_claims_the_kind_fetches_it_instead(self) -> None:
        # An animated WebP is not something FFmpeg can download, so the plugin
        # produces the file and FFmpeg is never started.
        produced = Path(self._temp.name) / "Clip.webm"
        produced.write_bytes(b"converted")

        with patch(
            "videotrack.core.ffmpeg_executor.postprocess_candidate", return_value=produced
        ), patch("videotrack.core.ffmpeg_executor.subprocess.Popen") as popen:
            result = FfmpegExecutor().run(
                self._request(_candidate("https://cdn.example.test/a.webp", "webp")),
                threading.Event(),
                self._record,
            )

        popen.assert_not_called()
        self.assertEqual(result, produced)
        self.assertEqual(self._kinds()[-1], DOWNLOAD_COMPLETED)


class StrictnessProbeTargetTests(unittest.TestCase):
    """The probe has to ask the same binary the command will run.

    Resolving it from the environment alone meant an operator who configured
    FFmpeg only in settings got no flags, so every playlist quietly took the
    slower fallback instead of downloading directly. Measured at roughly a
    seven-fold difference in throughput on a real stream.
    """

    def setUp(self) -> None:
        download_module.hls_strictness_flags.cache_clear()
        self.addCleanup(download_module.hls_strictness_flags.cache_clear)

    def test_the_configured_location_reaches_the_probe(self) -> None:
        with patch.object(download_module, "resolve_tool", return_value=None) as resolve:
            download_module.hls_strictness_flags(r"C:\ffmpeg\bin")

        resolve.assert_called_once()
        name, location = resolve.call_args.args
        self.assertEqual(name, "ffmpeg")
        self.assertEqual(location, Path(r"C:\ffmpeg\bin"))

    def test_no_location_falls_back_to_discovery(self) -> None:
        with patch.object(download_module, "resolve_tool", return_value=None) as resolve:
            download_module.hls_strictness_flags(None)

        self.assertIsNone(resolve.call_args.args[1])

    def test_an_unprobeable_ffmpeg_yields_no_flags(self) -> None:
        # Guessing a flag an older build lacks makes FFmpeg exit before it
        # downloads anything, so silence is the safe answer.
        with patch.object(download_module, "resolve_tool", return_value=None), patch.object(
            download_module.subprocess, "run", side_effect=OSError("no such binary")
        ):
            self.assertEqual(download_module.hls_strictness_flags("nowhere"), ())

    def test_the_command_builder_threads_the_location_through(self) -> None:
        with patch.object(
            download_module, "hls_strictness_flags", return_value=()
        ) as flags:
            build_ffmpeg_command(
                _capture(),
                _candidate("https://cdn.example.test/a.m3u8", "hls"),
                Path("out.mp4"),
                r"C:\ffmpeg\bin",
            )

        flags.assert_called_once_with(r"C:\ffmpeg\bin")

    def test_the_executor_passes_the_request_location(self) -> None:
        captured: dict = {}

        def fake_build(capture, candidate, out_file, ffmpeg_location=None):
            captured["location"] = ffmpeg_location
            return ["ffmpeg", "-i", candidate.url, str(out_file)]

        request = DownloadRequest(
            out_file=Path("out.mp4"),
            capture=_capture(),
            candidate=_candidate("https://cdn.example.test/a.m3u8", "hls"),
            ffmpeg_location=r"C:\ffmpeg\bin",
        )
        with patch(
            "videotrack.core.ffmpeg_executor.build_ffmpeg_command", side_effect=fake_build
        ), patch("videotrack.core.ffmpeg_executor.subprocess.Popen", _FailingProcess), patch(
            "videotrack.core.ffmpeg_executor.download_obfuscated_hls",
            side_effect=RuntimeError("stop here"),
        ):
            with self.assertRaises(RuntimeError):
                FfmpegExecutor().run(request, threading.Event(), lambda event: None)

        self.assertEqual(captured["location"], r"C:\ffmpeg\bin")


class RepackReportingTests(unittest.TestCase):
    """The repack must be usable from a server: no stdout, and interruptible."""

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.out_file = Path(self._temp.name) / "Clip.mp4"

    @staticmethod
    def _ts_payload() -> bytes:
        # Thirteen sync-byte-aligned packets, enough for the MPEG-TS check.
        return b"".join(b"\x47" + bytes(187) for _ in range(13))

    def _responses(self, segment_count: int):
        manifest = "#EXTM3U\n" + "\n".join(
            f"https://cdn.example.test/seg/{index}" for index in range(segment_count)
        )

        class _Response:
            def __init__(self, text: str = "", content: bytes = b"") -> None:
                self.ok = True
                self.status_code = 200
                self.text = text
                self.content = content

        calls = {"n": 0}

        def fake_get(url, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return _Response(text=manifest)
            return _Response(content=self._ts_payload())

        return fake_get

    def test_progress_is_reported_through_the_callback(self) -> None:
        reported: list[tuple[int, int]] = []

        with patch.object(download_module.requests, "get", side_effect=self._responses(3)), patch.object(
            download_module, "_run_command", return_value=0
        ):
            download_module.download_obfuscated_hls(
                _capture(),
                _candidate("https://cdn.example.test/a.m3u8", "hls"),
                self.out_file,
                on_progress=lambda done, total: reported.append((done, total)),
            )

        self.assertEqual(reported, [(1, 3), (2, 3), (3, 3)])

    def test_cancelling_stops_before_the_next_segment(self) -> None:
        cancel = threading.Event()

        def report(done: int, total: int) -> None:
            cancel.set()

        with patch.object(download_module.requests, "get", side_effect=self._responses(5)), patch.object(
            download_module, "_run_command", return_value=0
        ):
            with self.assertRaises(DownloadCancelled):
                download_module.download_obfuscated_hls(
                    _capture(),
                    _candidate("https://cdn.example.test/a.m3u8", "hls"),
                    self.out_file,
                    cancel=cancel,
                    on_progress=report,
                )

        self.assertFalse(self.out_file.with_suffix(".ts").exists())


if __name__ == "__main__":
    unittest.main()
