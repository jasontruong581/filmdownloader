"""Text that is not ASCII must not end a download.

Two failures observed on the same run, opposite directions, same cause - the
Windows ANSI codepage being used where UTF-8 was needed:

  * FFmpeg wrote a byte that is not valid in that codepage, and the thread
    reading its stderr died on the spot. The download then failed with no
    diagnosis, because the reader was gone before the reason was printed.
  * The CLI printed a Vietnamese page title and raised UnicodeEncodeError,
    ending the command before it downloaded anything.

A third defect surfaced with them: the command builder named `ffmpeg` and left
resolving it to the caller, so only the job executor ran the configured binary
and every other caller ran a bare name.
"""

from __future__ import annotations

import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from videotrack.core import download as download_module
from videotrack.core.download import build_ffmpeg_command
from videotrack.core.models import CaptureResult, StreamCandidate
from videotrack.core.preflight import TEXT_OUTPUT
from videotrack.logs import configure_console_encoding

OUT = Path("out.mp4")

#: Invalid in cp1252, which is what killed the reader thread.
UNDECODABLE = b"\x90\xff\xfe"


def _capture() -> CaptureResult:
    return CaptureResult(
        page_url="https://page.example.test/watch",
        final_url="https://page.example.test/watch",
        title="Đụ cô chị họ ngay bên cạnh chồng khi về quê",
        user_agent="test-agent",
        cookies={},
        requests=[],
    )


def _candidate() -> StreamCandidate:
    return StreamCandidate(
        url="https://cdn.example.test/a.m3u8", kind="hls", score=10, source="main"
    )


class ToolOutputDecodingTests(unittest.TestCase):
    """The policy has to survive bytes a locale codec would reject."""

    def test_bytes_no_ansi_codepage_can_decode_do_not_raise(self) -> None:
        # Python stands in for FFmpeg: the point is the bytes, not the tool.
        script = f"import sys; sys.stderr.buffer.write({UNDECODABLE!r}); sys.stderr.flush()"

        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, **TEXT_OUTPUT
        )

        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.stderr)

    def test_the_undecodable_bytes_become_replacements_not_an_exception(self) -> None:
        # A mangled glyph costs a character; an exception cost the diagnosis.
        script = f"import sys; sys.stderr.buffer.write({UNDECODABLE!r})"

        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, **TEXT_OUTPUT
        )

        self.assertIn("\ufffd", result.stderr)

    def test_reading_line_by_line_survives_them_too(self) -> None:
        # The reader runs on a thread, where an exception is not merely a lost
        # line: it is a dead reader and a failure with nothing to report.
        script = (
            "import sys; sys.stderr.buffer.write(b'first line\\n');"
            f"sys.stderr.buffer.write({UNDECODABLE!r} + b'\\n');"
            "sys.stderr.buffer.write(b'last line\\n')"
        )

        process = subprocess.Popen(
            [sys.executable, "-c", script], stderr=subprocess.PIPE, **TEXT_OUTPUT
        )
        lines = list(process.stderr)
        process.wait()

        self.assertIn("first line\n", lines)
        self.assertIn("last line\n", lines)

    def test_the_policy_names_a_codec_rather_than_trusting_the_locale(self) -> None:
        self.assertEqual(TEXT_OUTPUT["encoding"], "utf-8")
        self.assertEqual(TEXT_OUTPUT["errors"], "replace")


class ConsoleEncodingTests(unittest.TestCase):
    def test_a_stream_without_reconfigure_is_left_alone(self) -> None:
        # A test harness or a pipe wrapper need not support it, and must not be
        # a reason for the command to fail before it starts.
        captured = io.StringIO()
        with patch.object(sys, "stdout", captured), patch.object(sys, "stderr", captured):
            configure_console_encoding()  # must not raise

    def test_a_title_that_is_not_ascii_can_be_written(self) -> None:
        # The actual failure: printing the title, not downloading it.
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")

        with patch.object(sys, "stdout", stream), patch.object(sys, "stderr", stream):
            configure_console_encoding()
            print(_capture().title)
            sys.stdout.flush()

        self.assertIn("quê", buffer.getvalue().decode("utf-8"))

    def test_without_the_fix_that_same_write_raises(self) -> None:
        # Pins the premise rather than assuming it: this is what the command hit.
        buffer = io.BytesIO()
        stream = io.TextIOWrapper(buffer, encoding="cp1252", errors="strict")

        with self.assertRaises(UnicodeEncodeError):
            stream.write(_capture().title)
            stream.flush()


class CommandBinaryTests(unittest.TestCase):
    def test_the_builder_resolves_the_binary_itself(self) -> None:
        # The CLI and the pipeline pass no location and ran whatever argv[0]
        # said, which only works when FFmpeg happens to be on PATH.
        with patch.object(
            download_module, "resolve_tool", return_value=r"C:\ffmpeg\bin\ffmpeg.exe"
        ):
            cmd = build_ffmpeg_command(_capture(), _candidate(), OUT)

        self.assertEqual(cmd[0], r"C:\ffmpeg\bin\ffmpeg.exe")

    def test_an_unresolvable_binary_keeps_its_name(self) -> None:
        # So the missing-tool message still says which tool is missing.
        with patch.object(download_module, "resolve_tool", return_value=None):
            cmd = build_ffmpeg_command(_capture(), _candidate(), OUT)

        self.assertEqual(cmd[0], "ffmpeg")

    def test_the_configured_location_reaches_the_resolver(self) -> None:
        with patch.object(download_module, "resolve_tool", return_value="x") as resolver:
            build_ffmpeg_command(_capture(), _candidate(), OUT, r"C:\ffmpeg\bin")

        name, location = resolver.call_args_list[0].args
        self.assertEqual(name, "ffmpeg")
        self.assertEqual(Path(location), Path(r"C:\ffmpeg\bin"))

    def test_the_rest_of_the_command_is_unchanged(self) -> None:
        with patch.object(download_module, "resolve_tool", return_value="/usr/bin/ffmpeg"):
            cmd = build_ffmpeg_command(_capture(), _candidate(), OUT)

        self.assertEqual(cmd[1:5], ["-y", "-hide_banner", "-loglevel", "info"])
        self.assertEqual(cmd[-5:], ["-i", _candidate().url, "-c", "copy", str(OUT)])


if __name__ == "__main__":
    unittest.main()
