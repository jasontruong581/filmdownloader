"""Every path that needs FFmpeg has to be able to find it.

Observed: a repack downloaded all of its segments, reached 99.5%, and then
failed with "ffmpeg not found. Install it or set its location in settings." -
reported by the same FFmpeg that had already run the attempt the repack exists
to rescue. FFmpeg was configured in Settings and was not on PATH, and only some
call sites were told where it is.

`resolve_tool(name, None)` falls back to the FILMDOWNLOADER_FFMPEG variable, so
that variable is the channel a caller without a job request in scope has. The
settings file knew the answer and never put it there. Two fixes, because there
are two kinds of caller: one that holds a download request, and one that does
not.
"""

from __future__ import annotations

import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from videotrack.core import download as download_module
from videotrack.core import ffmpeg_executor as executor_module
from videotrack.core.download import download_obfuscated_hls
from videotrack.core.executor import DownloadRequest
from videotrack.core.ffmpeg_executor import FfmpegExecutor
from videotrack.core.models import CaptureResult, StreamCandidate
from videotrack.core.preflight import ENV_FFMPEG, resolve_tool
from videotrack.server.settings import Settings, publish_ffmpeg_location

#: The repack rejects a first segment that is not usable media, and it judges
#: that by sync bytes at repeated 188-byte offsets, so one packet is not enough
#: to look like the real thing.
TS_PACKET = (b"\x47\x40\x00\x10" + bytes(184)) * 12


def _capture() -> CaptureResult:
    return CaptureResult(
        page_url="https://page.example.test/watch",
        final_url="https://page.example.test/watch",
        title="Clip",
        user_agent="test-agent",
        cookies={},
        requests=[],
    )


def _candidate() -> StreamCandidate:
    return StreamCandidate(
        url="https://cdn.example.test/hls/master.m3u8", kind="hls", score=10, source="main"
    )


class _Response:
    def __init__(self, *, text: str = "", content: bytes = b"") -> None:
        self.ok = True
        self.status_code = 200
        self.text = text
        self.content = content


def _transport(manifest: str = "#EXTM3U\nseg1.ts\n"):
    """Serves the manifest first, then a segment for every later request."""

    def fake_get(url, headers=None, timeout=None):
        if url.endswith(".m3u8"):
            return _Response(text=manifest)
        return _Response(content=TS_PACKET)

    return fake_get


class RepackRemuxBinaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.out_file = Path(self._temp.name) / "Clip.mp4"
        self.commands: list[list[str]] = []

    def _run(self, ffmpeg_location: str | None, resolved: str | None):
        def fake_run(cmd):
            self.commands.append(cmd)
            return 0

        with patch.object(download_module, "requests") as requests_module, patch.object(
            download_module, "_run_command", side_effect=fake_run
        ), patch.object(download_module, "resolve_tool", return_value=resolved):
            requests_module.get.side_effect = _transport()
            return download_obfuscated_hls(
                _capture(), _candidate(), self.out_file, ffmpeg_location=ffmpeg_location
            )

    def test_the_remux_runs_the_configured_binary(self) -> None:
        # The reported failure: every segment downloaded, and this last step
        # asked for a bare `ffmpeg` that was never on PATH.
        self._run(r"C:\ffmpeg\bin", r"C:\ffmpeg\bin\ffmpeg.exe")

        self.assertEqual(self.commands[0][0], r"C:\ffmpeg\bin\ffmpeg.exe")

    def test_the_remux_does_not_fall_back_to_a_bare_name(self) -> None:
        self._run(r"C:\ffmpeg\bin", r"C:\ffmpeg\bin\ffmpeg.exe")

        self.assertNotEqual(self.commands[0][0], "ffmpeg")

    def test_the_location_is_the_one_the_resolver_was_asked_about(self) -> None:
        with patch.object(download_module, "requests") as requests_module, patch.object(
            download_module, "_run_command", return_value=0
        ), patch.object(download_module, "resolve_tool", return_value="x") as resolver:
            requests_module.get.side_effect = _transport()
            download_obfuscated_hls(
                _capture(), _candidate(), self.out_file, ffmpeg_location=r"C:\ffmpeg\bin"
            )

        name, location = resolver.call_args.args
        self.assertEqual(name, "ffmpeg")
        self.assertEqual(Path(location), Path(r"C:\ffmpeg\bin"))

    def test_an_unresolvable_tool_still_names_itself(self) -> None:
        # The bare name has to survive as a last resort so the missing-tool
        # message keeps saying which tool is missing.
        self._run(None, None)

        self.assertEqual(self.commands[0][0], "ffmpeg")

    def test_no_location_leaves_the_cli_contract_alone(self) -> None:
        # The CLI passes nothing and is configured through the variable, which
        # `resolve_tool` reads on its own.
        with patch.object(download_module, "requests") as requests_module, patch.object(
            download_module, "_run_command", return_value=0
        ), patch.object(download_module, "resolve_tool", return_value="ffmpeg") as resolver:
            requests_module.get.side_effect = _transport()
            download_obfuscated_hls(_capture(), _candidate(), self.out_file)

        self.assertIsNone(resolver.call_args.args[1])


class _FailingProcess:
    """FFmpeg exited non-zero, which is what sends the attempt to the repack."""

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


class ExecutorPassesTheLocationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.out_file = Path(self._temp.name) / "Clip.mp4"

        for name in ("hls_strictness_flags", "network_resilience_flags"):
            flags_patch = patch.object(download_module, name, return_value=())
            flags_patch.start()
            self.addCleanup(flags_patch.stop)

    def test_the_repack_is_told_where_ffmpeg_is(self) -> None:
        # The executor already held the location and used it for its own
        # command; the repack it falls back to was never given it.
        def fake_repack(capture, candidate, out_file, **kwargs):
            out_file.write_bytes(b"repacked")
            return out_file

        request = DownloadRequest(
            out_file=self.out_file,
            capture=_capture(),
            candidate=_candidate(),
            ffmpeg_location=r"C:\ffmpeg\bin",
        )

        with patch.object(executor_module.subprocess, "Popen", _FailingProcess), patch.object(
            executor_module, "download_obfuscated_hls", side_effect=fake_repack
        ) as repack:
            FfmpegExecutor().run(request, threading.Event(), lambda event: None)

        self.assertEqual(repack.call_args.kwargs["ffmpeg_location"], r"C:\ffmpeg\bin")


class PublishedLocationTests(unittest.TestCase):
    """The channel for a caller that has no download request in scope."""

    def setUp(self) -> None:
        self._saved = os.environ.get(ENV_FFMPEG)
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        if self._saved is None:
            os.environ.pop(ENV_FFMPEG, None)
        else:
            os.environ[ENV_FFMPEG] = self._saved

    def test_a_configured_location_is_published(self) -> None:
        os.environ.pop(ENV_FFMPEG, None)
        publish_ffmpeg_location(Settings(ffmpeg_location=r"C:\ffmpeg\bin"))

        self.assertEqual(os.environ[ENV_FFMPEG], r"C:\ffmpeg\bin")

    def test_an_empty_setting_clears_a_stale_value(self) -> None:
        # Leaving it would let `load_settings` fold the old value straight back
        # in, so clearing the setting would never take effect.
        os.environ[ENV_FFMPEG] = r"C:\old\bin"
        publish_ffmpeg_location(Settings(ffmpeg_location=""))

        self.assertNotIn(ENV_FFMPEG, os.environ)

    def test_surrounding_whitespace_is_not_published(self) -> None:
        os.environ.pop(ENV_FFMPEG, None)
        publish_ffmpeg_location(Settings(ffmpeg_location="  C:\\ffmpeg\\bin  "))

        self.assertEqual(os.environ[ENV_FFMPEG], r"C:\ffmpeg\bin")

    def test_a_location_only_in_settings_reaches_a_probe(self) -> None:
        # ffprobe and site plugins call `resolve_tool` with no location. This is
        # the whole point: with FFmpeg configured in Settings and absent from
        # PATH, they used to come back empty.
        with TemporaryDirectory() as fake_bin:
            binary = Path(fake_bin) / "ffprobe.exe"
            binary.write_bytes(b"")

            publish_ffmpeg_location(Settings(ffmpeg_location=fake_bin))
            found = resolve_tool("ffprobe")

        self.assertIsNotNone(found)
        self.assertEqual(Path(found).parent, Path(fake_bin))


if __name__ == "__main__":
    unittest.main()
