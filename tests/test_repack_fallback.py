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
        def fake_repack(capture, candidate, out_file, cancel=None, on_progress=None, ffmpeg_location=None):
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

        def fake_repack(capture, candidate, out_file, cancel=None, on_progress=None, ffmpeg_location=None):
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

        self.scratch = Path(self._temp.name) / "scratch"
        self.scratch.mkdir()
        patched = patch.object(download_module, "ensure_scratch_dir", return_value=self.scratch)
        patched.start()
        self.addCleanup(patched.stop)

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

        # Segments land in a workspace now rather than one growing `.ts`, and
        # cancelling has to take the whole of it with it.
        self.assertEqual(list(self.scratch.iterdir()), [])
        self.assertFalse(self.out_file.exists())


class _Served:
    """A tiny site: a URL map, and a record of what was asked for.

    The repack's whole job here is fetching, so what it fetched and with which
    headers is the thing worth asserting.
    """

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.requested: list[tuple[str, dict]] = []

    def get(self, url, headers=None, **kwargs):
        self.requested.append((url, dict(headers or {})))
        body = self.routes.get(url)
        if body is None:
            raise AssertionError(f"unexpected request for {url}")
        return _Reply(body)


class _Reply:
    def __init__(self, body) -> None:
        self.ok = True
        self.status_code = 206 if isinstance(body, tuple) else 200
        raw = body[0] if isinstance(body, tuple) else body
        if isinstance(raw, str):
            self.text = raw
            self.content = raw.encode("utf-8")
        else:
            self.text = ""
            self.content = raw


def _ts_bytes() -> bytes:
    return b"".join(b"\x47" + bytes(187) for _ in range(13))


class PlaylistAwareRepackTests(unittest.TestCase):
    """What the fallback does with the lines it used to throw away.

    Segments are written out as files and the playlist is rewritten against
    them, so FFmpeg does the AES-128 decryption and the fMP4 assembly it has
    always been able to do. What it could not do was fetch these segments.
    """

    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.out_file = self.root / "Clip.mp4"

        scratch = self.root / "scratch"
        scratch.mkdir()
        patched = patch.object(download_module, "ensure_scratch_dir", return_value=scratch)
        patched.start()
        self.addCleanup(patched.stop)

        #: Filled in by the fake remux, which runs while the workspace still
        #: exists - it is removed as soon as the repack returns.
        self.playlist_text = ""
        self.workspace: list[str] = []

    def _remux(self, cmd: list[str]) -> int:
        local = Path(cmd[cmd.index("-i") + 1])
        self.playlist_text = local.read_text(encoding="utf-8")
        self.workspace = sorted(path.name for path in local.parent.iterdir())
        Path(cmd[-1]).write_bytes(b"remuxed")
        return 0

    def _repack(self, served: _Served):
        with patch.object(download_module.requests, "get", served.get), patch.object(
            download_module, "_run_command", self._remux
        ):
            return download_module.download_obfuscated_hls(
                _capture(),
                _candidate("https://cdn.example.test/hls/index.m3u8", "hls"),
                self.out_file,
                on_progress=lambda done, total: None,
            )

    def _encrypted_site(self, master: bool = False) -> _Served:
        media = (
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:10\n"
            "#EXT-X-MEDIA-SEQUENCE:7\n"
            '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n'
            "#EXTINF:9.009,\nseg0.ts\n"
            "#EXTINF:9.009,\nseg1.ts\n"
            "#EXT-X-ENDLIST\n"
        )
        routes: dict[str, object] = {
            "https://cdn.example.test/hls/key.bin": b"0123456789abcdef",
            "https://cdn.example.test/hls/seg0.ts": _ts_bytes(),
            "https://cdn.example.test/hls/seg1.ts": _ts_bytes(),
        }
        if master:
            routes["https://cdn.example.test/hls/index.m3u8"] = (
                "#EXTM3U\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=800000,CODECS="avc1.4d401e,mp4a.40.2"\n'
                "low.m3u8\n"
                '#EXT-X-STREAM-INF:BANDWIDTH=2400000,CODECS="avc1.4d401f,mp4a.40.2"\n'
                "high.m3u8\n"
            )
            routes["https://cdn.example.test/hls/high.m3u8"] = media
        else:
            routes["https://cdn.example.test/hls/index.m3u8"] = media
        return _Served(routes)

    def test_the_key_is_fetched_and_saved_beside_the_segments(self) -> None:
        served = self._encrypted_site()

        self._repack(served)

        self.assertIn("https://cdn.example.test/hls/key.bin", [url for url, _ in served.requested])
        self.assertTrue(any(name.startswith("key-") for name in self.workspace))

    def test_the_local_playlist_keeps_the_key_line(self) -> None:
        # Without it the segments are written into a container as ciphertext,
        # and the download reports success.
        self._repack(self._encrypted_site())

        self.assertIn("#EXT-X-KEY:METHOD=AES-128", self.playlist_text)
        self.assertIn('URI="key-', self.playlist_text)

    def test_the_media_sequence_survives_the_rewrite(self) -> None:
        # A key with no explicit IV takes one from the sequence number.
        self._repack(self._encrypted_site())

        self.assertIn("#EXT-X-MEDIA-SEQUENCE:7", self.playlist_text)

    def test_the_key_is_fetched_once_however_many_segments_use_it(self) -> None:
        served = self._encrypted_site()

        self._repack(served)

        keys = [url for url, _ in served.requested if url.endswith("key.bin")]
        self.assertEqual(len(keys), 1)

    def test_a_master_playlist_is_followed_to_its_best_rendition(self) -> None:
        # It used to concatenate the variant playlists as if they were media.
        served = self._encrypted_site(master=True)

        self._repack(served)

        asked = [url for url, _ in served.requested]
        self.assertIn("https://cdn.example.test/hls/high.m3u8", asked)
        self.assertNotIn("https://cdn.example.test/hls/low.m3u8", asked)

    def test_an_init_segment_is_fetched_and_declared(self) -> None:
        served = _Served(
            {
                "https://cdn.example.test/hls/index.m3u8": (
                    "#EXTM3U\n"
                    "#EXT-X-TARGETDURATION:4\n"
                    '#EXT-X-MAP:URI="init.mp4"\n'
                    "#EXTINF:4.000,\nseg0.m4s\n"
                    "#EXT-X-ENDLIST\n"
                ),
                "https://cdn.example.test/hls/init.mp4": b"\x00\x00\x00\x18ftypiso5",
                "https://cdn.example.test/hls/seg0.m4s": b"\x00\x00\x00\x18stypmsdh",
            }
        )

        self._repack(served)

        self.assertIn("https://cdn.example.test/hls/init.mp4", [url for url, _ in served.requested])
        self.assertIn('#EXT-X-MAP:URI="init.bin"', self.playlist_text)
        self.assertIn("init.bin", self.workspace)

    def test_a_fragmented_segment_is_not_rejected_for_not_being_mpeg_ts(self) -> None:
        # The sanity check only means something for plaintext MPEG-TS. An fMP4
        # segment means nothing at all without the init segment beside it.
        served = _Served(
            {
                "https://cdn.example.test/hls/index.m3u8": (
                    "#EXTM3U\n"
                    '#EXT-X-MAP:URI="init.mp4"\n'
                    "#EXTINF:4.000,\nseg0.m4s\n"
                    "#EXT-X-ENDLIST\n"
                ),
                "https://cdn.example.test/hls/init.mp4": b"\x00\x00\x00\x18ftypiso5",
                "https://cdn.example.test/hls/seg0.m4s": b"not mpeg-ts at all",
            }
        )

        self.assertEqual(self._repack(served), self.out_file)

    def test_a_byte_ranged_segment_asks_for_its_slice(self) -> None:
        # Two segments, one resource. Fetching it whole twice writes the entire
        # file into the output once per slice.
        served = _Served(
            {
                "https://cdn.example.test/hls/index.m3u8": (
                    "#EXTM3U\n"
                    "#EXTINF:9.009,\n#EXT-X-BYTERANGE:2444@0\nall.ts\n"
                    "#EXTINF:9.009,\n#EXT-X-BYTERANGE:2444\nall.ts\n"
                    "#EXT-X-ENDLIST\n"
                ),
                "https://cdn.example.test/hls/all.ts": (_ts_bytes(),),
            }
        )

        self._repack(served)

        ranges = [headers.get("Range") for url, headers in served.requested if url.endswith("all.ts")]
        self.assertEqual(ranges, ["bytes=0-2443", "bytes=2444-4887"])

    def test_the_local_playlist_has_no_byterange_left(self) -> None:
        # Each slice is its own file now, so repeating the tag would slice it
        # a second time.
        served = _Served(
            {
                "https://cdn.example.test/hls/index.m3u8": (
                    "#EXTM3U\n"
                    "#EXTINF:9.009,\n#EXT-X-BYTERANGE:2444@0\nall.ts\n"
                    "#EXT-X-ENDLIST\n"
                ),
                "https://cdn.example.test/hls/all.ts": (_ts_bytes(),),
            }
        )

        self._repack(served)

        self.assertNotIn("BYTERANGE", self.playlist_text)

    def test_sample_aes_is_refused_rather_than_attempted(self) -> None:
        # It would otherwise produce a file of the right size that is noise.
        served = self._encrypted_site()
        served.routes["https://cdn.example.test/hls/index.m3u8"] = str(
            served.routes["https://cdn.example.test/hls/index.m3u8"]
        ).replace("METHOD=AES-128", "METHOD=SAMPLE-AES")

        with self.assertRaises(RuntimeError) as caught:
            self._repack(served)

        self.assertIn("SAMPLE-AES", str(caught.exception))

    def test_the_remux_is_told_to_accept_these_names(self) -> None:
        # The segments are saved under names that say nothing about their
        # contents, which is the state the strict demuxer refuses.
        captured: list[list[str]] = []

        def remux(cmd: list[str]) -> int:
            captured.append(cmd)
            return self._remux(cmd)

        served = self._encrypted_site()
        with patch.object(download_module.requests, "get", served.get), patch.object(
            download_module, "_run_command", remux
        ):
            download_module.download_obfuscated_hls(
                _capture(),
                _candidate("https://cdn.example.test/hls/index.m3u8", "hls"),
                self.out_file,
                on_progress=lambda done, total: None,
            )

        cmd = captured[0]
        self.assertIn("-allowed_extensions", cmd)
        self.assertEqual(cmd[cmd.index("-allowed_extensions") + 1], "ALL")
        self.assertIn("crypto", cmd[cmd.index("-protocol_whitelist") + 1])

    def test_the_workspace_is_removed_afterwards(self) -> None:
        self._repack(self._encrypted_site())

        leftovers = list((self.root / "scratch").iterdir())
        self.assertEqual(leftovers, [])

    def test_the_workspace_is_removed_when_the_remux_fails(self) -> None:
        served = self._encrypted_site()
        with patch.object(download_module.requests, "get", served.get), patch.object(
            download_module, "_run_command", return_value=1
        ):
            with self.assertRaises(RuntimeError):
                download_module.download_obfuscated_hls(
                    _capture(),
                    _candidate("https://cdn.example.test/hls/index.m3u8", "hls"),
                    self.out_file,
                    on_progress=lambda done, total: None,
                )

        self.assertEqual(list((self.root / "scratch").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
