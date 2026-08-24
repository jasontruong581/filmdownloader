"""Characterization tests for output filename derivation.

Naming is load-bearing beyond cosmetics: `batch_run_csv.has_downloaded_file`
skips work by globbing `{page_id}-*.mp4`, so a change to these rules silently
re-downloads or silently skips. Pinned before the vlxx metadata extraction moves
into a site plugin.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from videotrack.core.download import _extract_page_id, _safe_name, output_path_for, unique_path
from videotrack.sites.quatvn import asset_suffix
from videotrack.core.models import CaptureResult


def _capture(title: str = "", final_url: str = "https://page.example.test/watch/123") -> CaptureResult:
    return CaptureResult(
        page_url=final_url,
        final_url=final_url,
        title=title,
        user_agent="test-agent",
        cookies={},
        requests=[],
    )


class SafeNameTests(unittest.TestCase):
    def test_illegal_path_characters_become_underscores(self) -> None:
        self.assertEqual(_safe_name('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_whitespace_is_collapsed_and_trimmed(self) -> None:
        self.assertEqual(_safe_name("  a   b  "), "a b")

    def test_empty_input_becomes_video(self) -> None:
        self.assertEqual(_safe_name("   "), "video")

    def test_name_is_truncated_to_120_characters(self) -> None:
        self.assertEqual(len(_safe_name("x" * 200)), 120)


class OutputPathTests(unittest.TestCase):
    def test_preferred_base_wins_over_capture_title(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = output_path_for(_capture(title="Page Title"), Path(temp_dir), preferred_base="ABC-123")

            self.assertEqual(path.name, "ABC-123.mp4")

    def test_capture_title_is_used_when_no_preferred_base(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = output_path_for(_capture(title="Page Title"), Path(temp_dir))

            self.assertEqual(path.name, "Page Title.mp4")

    def test_url_path_name_is_the_last_fallback(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = output_path_for(_capture(final_url="https://page.example.test/watch/123"), Path(temp_dir))

            self.assertEqual(path.name, "123.mp4")

    def test_video_is_used_when_the_url_has_no_path_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = output_path_for(_capture(final_url="https://page.example.test/"), Path(temp_dir))

            self.assertEqual(path.name, "video.mp4")

    def test_an_existing_mp4_suffix_is_not_doubled(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = output_path_for(_capture(), Path(temp_dir), preferred_base="clip.mp4")

            self.assertEqual(path.name, "clip.mp4")

    def test_suffix_check_is_case_insensitive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = output_path_for(_capture(), Path(temp_dir), preferred_base="clip.MP4")

            self.assertEqual(path.name, "clip.MP4")

    def test_illegal_characters_in_the_title_are_sanitized(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = output_path_for(_capture(title="a/b:c"), Path(temp_dir))

            self.assertEqual(path.name, "a_b_c.mp4")

    def test_truncation_happens_before_the_extension_is_appended(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = output_path_for(_capture(), Path(temp_dir), preferred_base="x" * 200)

            self.assertEqual(len(path.name), 124)

    def test_the_output_directory_is_created(self) -> None:
        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "nested" / "dir"

            output_path_for(_capture(title="Clip"), target)

            self.assertTrue(target.is_dir())

    def test_two_items_sharing_a_title_get_distinct_paths_once_written(self) -> None:
        with TemporaryDirectory() as temp_dir:
            first = unique_path(output_path_for(_capture(title="Same"), Path(temp_dir)))
            first.write_bytes(b"x")
            second = unique_path(output_path_for(_capture(title="Same"), Path(temp_dir)))

            self.assertNotEqual(first, second)
            self.assertEqual(second.name, "Same (2).mp4")


class PageIdTests(unittest.TestCase):
    def test_numeric_last_segment_is_the_page_id(self) -> None:
        self.assertEqual(_extract_page_id("https://page.example.test/watch/12345"), "12345")

    def test_trailing_slash_still_yields_the_numeric_segment(self) -> None:
        self.assertEqual(_extract_page_id("https://page.example.test/watch/12345/"), "12345")

    def test_non_numeric_last_segment_yields_none(self) -> None:
        self.assertIsNone(_extract_page_id("https://page.example.test/watch/some-slug"))

    def test_a_pathless_url_yields_none(self) -> None:
        self.assertIsNone(_extract_page_id("https://page.example.test/"))


class UniquePathTests(unittest.TestCase):
    def test_a_free_path_is_returned_unchanged(self) -> None:
        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "clip.mp4"

            self.assertEqual(unique_path(target), target)

    def test_an_existing_path_gets_a_numeric_suffix(self) -> None:
        with TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "clip.mp4"
            target.write_bytes(b"x")

            self.assertEqual(unique_path(target).name, "clip (2).mp4")

    def test_suffixes_keep_climbing_past_a_taken_number(self) -> None:
        with TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "clip.mp4").write_bytes(b"x")
            (Path(temp_dir) / "clip (2).mp4").write_bytes(b"x")

            self.assertEqual(unique_path(Path(temp_dir) / "clip.mp4").name, "clip (3).mp4")


class QuatvnAssetSuffixTests(unittest.TestCase):
    def test_numbered_webp_becomes_a_zero_padded_clip_suffix(self) -> None:
        self.assertEqual(asset_suffix("https://cdn.example.test/stream/name%20(7).webp"), "clip-07")

    def test_two_digit_number_is_preserved(self) -> None:
        self.assertEqual(asset_suffix("https://cdn.example.test/stream/name%20(12).webp"), "clip-12")

    def test_unnumbered_asset_falls_back_to_its_stem(self) -> None:
        self.assertEqual(asset_suffix("https://cdn.example.test/stream/single.webp"), "single")


if __name__ == "__main__":
    unittest.main()
