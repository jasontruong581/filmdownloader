"""FFmpeg progress parsing, driven by recorded output. No FFmpeg needed."""

from __future__ import annotations

import unittest

from videotrack.core.events import (
    PHASE_DOWNLOADING_AUDIO,
    PHASE_DOWNLOADING_VIDEO,
    PHASE_MERGING,
    MonotonicProgress,
    Progress,
)
from videotrack.core.ffmpeg_progress import FfmpegProgressParser

# One real -progress block, as FFmpeg writes it.
BLOCK = """bitrate=1234.5kbits/s
total_size=5242880
out_time_us=30000000
out_time=00:00:30.000000
speed=12.5x
progress=continue
"""

END_BLOCK = """total_size=10485760
out_time_us=60000000
speed=12.5x
progress=end
"""


def _feed(parser: FfmpegProgressParser, block: str) -> Progress | None:
    sample = None
    for line in block.splitlines():
        result = parser.feed(line)
        if result is not None:
            sample = result
    return sample


class BlockParsingTests(unittest.TestCase):
    def test_a_block_yields_one_sample_on_the_progress_line(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=120.0)
        samples = [parser.feed(line) for line in BLOCK.splitlines()]

        self.assertEqual(sum(1 for s in samples if s is not None), 1)

    def test_percent_comes_from_elapsed_media_time(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=120.0)

        sample = _feed(parser, BLOCK)

        self.assertEqual(sample.percent, 25.0)

    def test_bytes_written_are_reported_but_not_as_a_total(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=120.0)

        sample = _feed(parser, BLOCK)

        self.assertEqual(sample.downloaded_bytes, 5242880)
        # FFmpeg's total_size is bytes-so-far; claiming it as a final size lies.
        self.assertIsNone(sample.total_bytes)

    def test_speed_is_derived_in_bytes_per_second(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=120.0)

        sample = _feed(parser, BLOCK)

        # 5242880 bytes over 30s of media at 12.5x wall-clock = 2.4s elapsed.
        self.assertAlmostEqual(sample.speed_bps, 5242880 / (30.0 / 12.5), places=2)

    def test_eta_is_reported_while_in_flight(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=120.0)

        sample = _feed(parser, BLOCK)

        self.assertAlmostEqual(sample.eta_seconds, 90.0 / 12.5, places=4)

    def test_the_end_block_reports_one_hundred_percent(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=120.0)

        sample = _feed(parser, END_BLOCK)

        self.assertEqual(sample.percent, 100.0)
        self.assertIsNone(sample.eta_seconds)

    def test_consecutive_blocks_each_yield_a_sample(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=120.0)

        first = _feed(parser, BLOCK)
        second = _feed(parser, END_BLOCK)

        self.assertEqual((first.percent, second.percent), (25.0, 100.0))


class UnknownDurationTests(unittest.TestCase):
    def test_percent_is_none_without_a_duration(self) -> None:
        # Some HLS streams report no duration. Zero would look like a stall.
        parser = FfmpegProgressParser(duration_seconds=None)

        sample = _feed(parser, BLOCK)

        self.assertIsNone(sample.percent)
        self.assertEqual(sample.downloaded_bytes, 5242880)

    def test_a_zero_duration_is_treated_as_unknown(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=0.0)

        self.assertIsNone(_feed(parser, BLOCK).percent)

    def test_percent_is_clamped_when_elapsed_exceeds_duration(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=10.0)

        self.assertEqual(_feed(parser, BLOCK).percent, 100.0)


class MalformedInputTests(unittest.TestCase):
    def test_lines_without_an_equals_sign_are_ignored(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=120.0)

        self.assertIsNone(parser.feed("this is not a progress line"))
        self.assertIsNone(parser.feed(""))

    def test_unparseable_numbers_do_not_raise(self) -> None:
        parser = FfmpegProgressParser(duration_seconds=120.0)

        sample = _feed(parser, "out_time_us=nonsense\ntotal_size=also-bad\nspeed=weird\nprogress=continue\n")

        self.assertIsNone(sample.percent)
        self.assertIsNone(sample.downloaded_bytes)


class SplitFormatAggregationTests(unittest.TestCase):
    def test_a_split_download_reports_one_monotonic_track(self) -> None:
        # A yt-dlp DASH pick runs video to 100, audio to 100, then merges.
        # Passed through raw, the bar would reset twice.
        folder = MonotonicProgress()
        raw = [
            (PHASE_DOWNLOADING_VIDEO, 0.0),
            (PHASE_DOWNLOADING_VIDEO, 100.0),
            (PHASE_DOWNLOADING_AUDIO, 0.0),
            (PHASE_DOWNLOADING_AUDIO, 100.0),
            (PHASE_MERGING, 100.0),
        ]

        folded = [folder.fold(Progress(phase=phase, percent=percent)).percent for phase, percent in raw]

        self.assertEqual(folded, sorted(folded))
        self.assertLessEqual(max(folded), 100.0)
        self.assertGreater(folded[-1], folded[0])

    def test_a_later_lower_sample_never_moves_the_bar_backwards(self) -> None:
        folder = MonotonicProgress()
        folder.fold(Progress(phase=PHASE_DOWNLOADING_VIDEO, percent=100.0))

        after = folder.fold(Progress(phase=PHASE_DOWNLOADING_VIDEO, percent=10.0))

        self.assertEqual(after.percent, 80.0)

    def test_phase_is_carried_through_for_display(self) -> None:
        folder = MonotonicProgress()

        folded = folder.fold(Progress(phase=PHASE_MERGING, percent=50.0))

        self.assertEqual(folded.phase, PHASE_MERGING)


if __name__ == "__main__":
    unittest.main()
