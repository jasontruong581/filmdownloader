"""External tool discovery must be honest about what it found and where.

FFmpeg is commonly absent from PATH on Windows, so the configured-location path
matters as much as the PATH lookup.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from videotrack.core import preflight
from videotrack import engines


class ToolStatusTests(unittest.TestCase):
    def test_a_missing_required_tool_is_blocking(self) -> None:
        status = preflight.ToolStatus(name="ffmpeg", required=True, path=None, version=None)

        self.assertFalse(status.available)
        self.assertTrue(status.blocking)

    def test_a_missing_optional_tool_is_not_blocking(self) -> None:
        status = preflight.ToolStatus(name="magick", required=False, path=None, version=None)

        self.assertFalse(status.blocking)

    def test_a_present_required_tool_is_not_blocking(self) -> None:
        status = preflight.ToolStatus(name="ffmpeg", required=True, path="/usr/bin/ffmpeg", version="v1")

        self.assertTrue(status.available)
        self.assertFalse(status.blocking)


class ResolveToolTests(unittest.TestCase):
    def test_path_lookup_is_used_when_no_location_is_configured(self) -> None:
        with patch("videotrack.core.preflight.shutil.which", return_value="/usr/bin/ffmpeg") as which:
            self.assertEqual(preflight.resolve_tool("ffmpeg", None), "/usr/bin/ffmpeg")

        which.assert_called_with("ffmpeg")

    def test_a_configured_directory_is_searched_before_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            location = Path(temp_dir)

            def fake_which(name, path=None):
                return f"{temp_dir}/{name}.exe" if path == temp_dir else "/usr/bin/ffmpeg"

            with patch("videotrack.core.preflight.shutil.which", side_effect=fake_which):
                resolved = preflight.resolve_tool("ffmpeg", location)

            self.assertEqual(resolved, f"{temp_dir}/ffmpeg.exe")

    def test_a_configured_executable_is_used_directly(self) -> None:
        with TemporaryDirectory() as temp_dir:
            exe = Path(temp_dir) / "ffmpeg.exe"
            exe.write_bytes(b"")

            resolved = preflight.resolve_tool("ffmpeg", exe)

            self.assertEqual(resolved, str(exe))

    def test_a_configured_location_does_not_apply_to_unrelated_tools(self) -> None:
        with TemporaryDirectory() as temp_dir:
            with patch("videotrack.core.preflight.shutil.which", return_value="/usr/bin/magick") as which:
                resolved = preflight.resolve_tool("magick", Path(temp_dir))

            self.assertEqual(resolved, "/usr/bin/magick")
            which.assert_called_once_with("magick")

    def test_a_configured_executable_with_the_wrong_name_falls_back_to_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            exe = Path(temp_dir) / "something-else.exe"
            exe.write_bytes(b"")

            with patch("videotrack.core.preflight.shutil.which", return_value="/usr/bin/ffmpeg"):
                self.assertEqual(preflight.resolve_tool("ffmpeg", exe), "/usr/bin/ffmpeg")


class CheckToolsTests(unittest.TestCase):
    def test_every_declared_tool_is_reported(self) -> None:
        with patch("videotrack.core.preflight.resolve_tool", return_value=None):
            statuses = preflight.check_tools()

        self.assertEqual([s.name for s in statuses], [name for name, _ in preflight.TOOLS])
        self.assertTrue(all(not s.available for s in statuses))

    def test_ffmpeg_and_ffprobe_are_the_required_tools(self) -> None:
        required = {name for name, is_required in preflight.TOOLS if is_required}

        self.assertEqual(required, {"ffmpeg", "ffprobe"})

    def test_report_names_the_resolved_path(self) -> None:
        statuses = (
            preflight.ToolStatus(name="ffmpeg", required=True, path="/opt/ffmpeg", version="ffmpeg version 7"),
            preflight.ToolStatus(name="magick", required=False, path=None, version=None),
        )

        report = preflight.format_report(statuses)

        self.assertIn("/opt/ffmpeg", report)
        self.assertIn("ffmpeg version 7", report)
        self.assertIn("optional", report)

    def test_report_marks_a_missing_required_tool(self) -> None:
        statuses = (preflight.ToolStatus(name="ffmpeg", required=True, path=None, version=None),)

        self.assertIn("MISS", preflight.format_report(statuses))


class YtdlpVersionTests(unittest.TestCase):
    def test_the_check_degrades_instead_of_raising(self) -> None:
        # yt-dlp is optional, so this must answer either way without failing.
        version = engines.ytdlp_version()

        self.assertTrue(version is None or isinstance(version, str))

    def test_an_absent_ytdlp_reports_none(self) -> None:
        with patch.dict("sys.modules", {"yt_dlp": None}):
            self.assertIsNone(engines.ytdlp_version())


if __name__ == "__main__":
    unittest.main()
