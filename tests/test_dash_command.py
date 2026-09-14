"""What the command builder owes a DASH manifest.

`.mpd` has been detected and ranked since candidates were first scored, and
then handed to FFmpeg as if it were a plain file. It is not: the DASH demuxer
enforces the same two rules the HLS demuxer does, and only HLS was being asked.

The rule that matters is the extension gate. The DASH default is the narrower
of the two - `aac,m4a,m4s,m4v,mov,mp4,webm,ts` - so a CDN serving segments from
a path with no extension is refused with "blocked for security reasons" and no
download at all.

What DASH must *not* get is the other half. `-extension_picky` is an HLS
demuxer option, and FFmpeg answers it on a DASH input with "Option not found"
and opens nothing, so relaxing both demuxers the same way would take DASH from
partly working to not working.

No FFmpeg here: this is the command, and the capability probe is stubbed.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from videotrack.core import download as download_module
from videotrack.core.download import (
    build_ffmpeg_command,
    is_adaptive_candidate,
    is_dash_candidate,
    is_hls_candidate,
)
from videotrack.core.models import CaptureResult, StreamCandidate

OUT = Path("output") / "clip.mp4"

#: What the HLS probe returns on a build that has the option.
STRICTNESS = ("-extension_picky", "0")


def _capture() -> CaptureResult:
    return CaptureResult(
        page_url="https://page.example.test/watch",
        final_url="https://page.example.test/watch",
        title="Clip",
        user_agent="test-agent",
        cookies={},
        requests=[],
    )


def _candidate(url: str, kind: str) -> StreamCandidate:
    return StreamCandidate(url=url, kind=kind, score=80, source="test")


class RecognitionTests(unittest.TestCase):
    def test_an_mpd_url_is_dash(self) -> None:
        self.assertTrue(is_dash_candidate(_candidate("https://cdn.example.test/a.mpd", "mp4")))

    def test_a_dash_kind_is_dash_whatever_the_url_says(self) -> None:
        self.assertTrue(is_dash_candidate(_candidate("https://cdn.example.test/manifest?id=7", "dash")))

    def test_a_plain_file_is_not_dash(self) -> None:
        self.assertFalse(is_dash_candidate(_candidate("https://cdn.example.test/a.mp4", "mp4")))

    def test_dash_and_hls_are_both_adaptive(self) -> None:
        self.assertTrue(is_adaptive_candidate(_candidate("https://cdn.example.test/a.mpd", "dash")))
        self.assertTrue(is_adaptive_candidate(_candidate("https://cdn.example.test/a.m3u8", "hls")))

    def test_dash_is_not_mistaken_for_a_playlist(self) -> None:
        # They need different flags, so the two questions have to stay separate.
        self.assertFalse(is_hls_candidate(_candidate("https://cdn.example.test/a.mpd", "dash")))

    def test_a_plain_file_is_not_adaptive(self) -> None:
        self.assertFalse(is_adaptive_candidate(_candidate("https://cdn.example.test/a.mp4", "mp4")))


class CommandTests(unittest.TestCase):
    def setUp(self) -> None:
        for name, value in (
            ("hls_strictness_flags", STRICTNESS),
            ("network_resilience_flags", ()),
        ):
            probe = patch.object(download_module, name, return_value=value)
            probe.start()
            self.addCleanup(probe.stop)

        resolver = patch.object(download_module, "resolve_tool", return_value=None)
        resolver.start()
        self.addCleanup(resolver.stop)

    def _build(self, url: str, kind: str) -> list[str]:
        return build_ffmpeg_command(_capture(), _candidate(url, kind), OUT)

    def test_a_manifest_is_allowed_any_segment_extension(self) -> None:
        # The regression: a CDN with no extension on its segment paths was
        # refused outright, and the message said "blocked for security reasons".
        cmd = self._build("https://cdn.example.test/a.mpd", "dash")

        self.assertIn("-allowed_extensions", cmd)
        self.assertEqual(cmd[cmd.index("-allowed_extensions") + 1], "ALL")

    def test_a_manifest_gets_the_protocol_whitelist(self) -> None:
        cmd = self._build("https://cdn.example.test/a.mpd", "dash")

        self.assertIn("https", cmd[cmd.index("-protocol_whitelist") + 1])

    def test_a_manifest_does_not_get_the_hls_demuxer_options(self) -> None:
        # FFmpeg answers `-extension_picky` on a DASH input with "Option not
        # found" and opens nothing at all, so this would take DASH from partly
        # working to not working.
        cmd = self._build("https://cdn.example.test/a.mpd", "dash")

        self.assertNotIn("-extension_picky", cmd)

    def test_a_playlist_still_gets_both(self) -> None:
        cmd = self._build("https://cdn.example.test/a.m3u8", "hls")

        self.assertEqual(cmd[cmd.index("-allowed_extensions") + 1], "ALL")
        self.assertIn("-extension_picky", cmd)

    def test_a_plain_file_gets_neither(self) -> None:
        cmd = self._build("https://cdn.example.test/a.mp4", "mp4")

        self.assertNotIn("-allowed_extensions", cmd)
        self.assertNotIn("-protocol_whitelist", cmd)

    def test_the_flags_precede_the_input(self) -> None:
        # They are input options; after `-i` they apply to the output and the
        # demuxer never sees them.
        cmd = self._build("https://cdn.example.test/a.mpd", "dash")

        self.assertLess(cmd.index("-allowed_extensions"), cmd.index("-i"))
        self.assertLess(cmd.index("-protocol_whitelist"), cmd.index("-i"))

    def test_the_manifest_is_still_the_input(self) -> None:
        cmd = self._build("https://cdn.example.test/a.mpd", "dash")

        self.assertEqual(cmd[cmd.index("-i") + 1], "https://cdn.example.test/a.mpd")


if __name__ == "__main__":
    unittest.main()
