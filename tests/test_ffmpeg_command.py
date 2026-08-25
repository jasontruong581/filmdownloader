"""Characterization tests for the FFmpeg command builder.

The header block and the HLS-only protocol flags are what make an authorized
request succeed. They are pinned here before the download module is split into a
neutral executor and site-specific postprocessors.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from videotrack.core.download import _headers_block, build_ffmpeg_command
from videotrack.core.ffmpeg_executor import _resolved_binary
from videotrack.core.models import CaptureResult, StreamCandidate

OUT = Path("output") / "clip.mp4"


def _capture(
    *,
    user_agent: str = "test-agent",
    cookies: dict[str, str] | None = None,
    final_url: str = "https://page.example.test/watch",
) -> CaptureResult:
    return CaptureResult(
        page_url=final_url,
        final_url=final_url,
        title="Example",
        user_agent=user_agent,
        cookies=cookies or {},
        requests=[],
    )


def _candidate(url: str = "https://cdn.example.test/a.mp4", *, kind: str = "mp4", referer: str | None = None) -> StreamCandidate:
    return StreamCandidate(url=url, kind=kind, score=80, source="test", referer=referer)


class HeaderBlockTests(unittest.TestCase):
    def test_referer_falls_back_to_the_capture_final_url(self) -> None:
        self.assertEqual(_headers_block(_capture()), "Referer: https://page.example.test/watch\r\n")

    def test_explicit_referer_overrides_the_final_url(self) -> None:
        block = _headers_block(_capture(), "https://player.example.test/embed")

        self.assertEqual(block, "Referer: https://player.example.test/embed\r\n")

    def test_cookies_are_joined_with_semicolons(self) -> None:
        block = _headers_block(_capture(cookies={"a": "1", "b": "2"}))

        self.assertEqual(block, "Referer: https://page.example.test/watch\r\nCookie: a=1; b=2\r\n")

    def test_no_referer_and_no_cookies_yields_an_empty_block(self) -> None:
        self.assertEqual(_headers_block(_capture(final_url="")), "")


class CommandShapeTests(unittest.TestCase):
    def test_command_starts_with_the_fixed_prefix(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate(), OUT)

        self.assertEqual(cmd[:5], ["ffmpeg", "-y", "-hide_banner", "-loglevel", "info"])

    def test_command_ends_with_input_copy_and_output(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate(), OUT)

        self.assertEqual(cmd[-5:], ["-i", "https://cdn.example.test/a.mp4", "-c", "copy", str(OUT)])

    def test_user_agent_is_passed_as_a_flag(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate(), OUT)

        self.assertEqual(cmd[cmd.index("-user_agent") + 1], "test-agent")

    def test_absent_user_agent_omits_the_flag(self) -> None:
        cmd = build_ffmpeg_command(_capture(user_agent=""), _candidate(), OUT)

        self.assertNotIn("-user_agent", cmd)

    def test_headers_flag_carries_the_header_block(self) -> None:
        cmd = build_ffmpeg_command(_capture(cookies={"s": "1"}), _candidate(referer="https://r.example.test/"), OUT)

        self.assertEqual(cmd[cmd.index("-headers") + 1], "Referer: https://r.example.test/\r\nCookie: s=1\r\n")

    def test_empty_header_block_omits_the_flag(self) -> None:
        cmd = build_ffmpeg_command(_capture(final_url=""), _candidate(), OUT)

        self.assertNotIn("-headers", cmd)


class HlsFlagTests(unittest.TestCase):
    HLS_FLAGS = ["-protocol_whitelist", "file,http,https,tcp,tls,crypto,data", "-allowed_extensions", "ALL"]

    def test_mp4_candidate_gets_no_hls_flags(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate(), OUT)

        self.assertNotIn("-protocol_whitelist", cmd)

    def test_hls_kind_adds_the_flags(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate(kind="hls"), OUT)

        self.assertEqual(cmd[cmd.index("-protocol_whitelist") : cmd.index("-protocol_whitelist") + 4], self.HLS_FLAGS)

    def test_playlist_kind_adds_the_flags(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate(kind="playlist"), OUT)

        self.assertIn("-allowed_extensions", cmd)

    def test_m3u8_in_the_url_adds_the_flags_even_for_an_mp4_kind(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate("https://cdn.example.test/a.m3u8", kind="mp4"), OUT)

        self.assertIn("-protocol_whitelist", cmd)

    def test_manifest_in_the_url_adds_the_flags(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate("https://cdn.example.test/manifest", kind="media"), OUT)

        self.assertIn("-protocol_whitelist", cmd)

    def test_hls_flags_precede_the_input_flag(self) -> None:
        cmd = build_ffmpeg_command(_capture(), _candidate(kind="hls"), OUT)

        self.assertLess(cmd.index("-protocol_whitelist"), cmd.index("-i"))


if __name__ == "__main__":
    unittest.main()


class ResolvedBinaryTests(unittest.TestCase):
    """How a configured FFmpeg location reaches argv[0].

    The setting names a directory or the executable. `preflight.resolve_tool`
    has always accepted both; the executor used to substitute the raw value, so
    a directory - which is what the documented env var and the Settings field
    both suggest - produced a command that could not run.
    """

    def _install_fake_ffmpeg(self, directory: Path) -> Path:
        binary = directory / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        binary.write_text("")
        binary.chmod(0o755)
        return binary

    def test_a_directory_resolves_to_the_executable_inside_it(self) -> None:
        with TemporaryDirectory() as temp:
            binary = self._install_fake_ffmpeg(Path(temp))

            cmd = _resolved_binary(["ffmpeg", "-i", "in.m3u8"], temp)

            self.assertEqual(Path(cmd[0]), binary)
            self.assertEqual(cmd[1:], ["-i", "in.m3u8"])

    def test_an_executable_path_is_honored_as_given(self) -> None:
        with TemporaryDirectory() as temp:
            binary = self._install_fake_ffmpeg(Path(temp))

            cmd = _resolved_binary(["ffmpeg", "-i", "in.m3u8"], str(binary))

            self.assertEqual(Path(cmd[0]), binary)

    def test_an_unusable_location_leaves_the_command_alone(self) -> None:
        # The missing-tool error has to stay reachable rather than being masked.
        with TemporaryDirectory() as temp:
            cmd = _resolved_binary(["ffmpeg", "-i", "in.m3u8"], str(Path(temp) / "absent"))

            self.assertEqual(cmd[0], "ffmpeg")

    def test_no_location_leaves_the_command_alone(self) -> None:
        self.assertEqual(_resolved_binary(["ffmpeg", "-y"], None), ["ffmpeg", "-y"])
