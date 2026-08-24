"""The CSV batch runner skips finished work by globbing the derived filename.

That makes output naming load-bearing beyond cosmetics: a naming change that
nobody notices here silently re-downloads everything, or silently skips
everything. This test states the coupling so it cannot be broken quietly.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from batch_run_csv import has_downloaded_file  # noqa: E402

from videotrack.core.download import _extract_page_id, output_path_for, unique_path  # noqa: E402
from videotrack.core.models import CaptureResult  # noqa: E402


def _capture(url: str) -> CaptureResult:
    return CaptureResult(
        page_url=url,
        final_url=url,
        title="Example",
        user_agent="test-agent",
        cookies={},
        requests=[],
    )


class NamingCouplingTests(unittest.TestCase):
    def test_the_page_id_in_the_filename_is_what_the_resume_glob_matches(self) -> None:
        page_url = "https://page.example.test/watch/12345"
        page_id = _extract_page_id(page_url)
        self.assertEqual(page_id, "12345")

        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            # This is the shape a site plugin's metadata produces.
            produced = output_path_for(_capture(page_url), output_dir, preferred_base=f"{page_id}-ABC-001")
            produced.write_bytes(b"x")

            self.assertTrue(has_downloaded_file(output_dir, page_id))

    def test_a_collision_suffixed_file_still_satisfies_the_resume_check(self) -> None:
        # unique_path appends " (2)" rather than overwriting; the glob must still
        # recognize the work as done.
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            first = output_path_for(_capture("https://page.example.test/watch/777"), output_dir, preferred_base="777-ABC")
            first.write_bytes(b"x")
            second = unique_path(first)
            second.write_bytes(b"x")

            self.assertEqual(second.name, "777-ABC (2).mp4")
            self.assertTrue(has_downloaded_file(output_dir, "777"))

    def test_an_unrelated_file_does_not_satisfy_the_resume_check(self) -> None:
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            (output_dir / "99999-ABC.mp4").write_bytes(b"x")

            self.assertFalse(has_downloaded_file(output_dir, "12345"))

    def test_an_empty_id_never_counts_as_downloaded(self) -> None:
        with TemporaryDirectory() as temp_dir:
            self.assertFalse(has_downloaded_file(Path(temp_dir), ""))

    def test_a_non_numeric_url_tail_yields_no_page_id(self) -> None:
        # Such a page falls back to title-based naming, and the CSV runner then
        # relies on its own id column rather than on the page id.
        self.assertIsNone(_extract_page_id("https://page.example.test/watch/some-slug"))


if __name__ == "__main__":
    unittest.main()
