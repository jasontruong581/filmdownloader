from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from videotrack.sites.flowplayer import CollectionVideo, _existing_file_matches_source, parse_flowplayer_collection


class FlowplayerCollectionTests(unittest.TestCase):
    def test_parses_ordered_unique_data_items(self) -> None:
        page_html = """
        <html><title>Example Collection • Site</title>
        <div data-item="{&quot;fv_title&quot;:&quot;Clip One&quot;,&quot;sources&quot;:[{&quot;src&quot;:&quot;https://cdn.example.test/one.mp4&quot;}]}" />
        <div data-item="{&quot;fv_title&quot;:&quot;Clip Two&quot;,&quot;sources&quot;:[{&quot;src&quot;:&quot;https://cdn.example.test/two.m3u8&quot;}]}" />
        <div data-item="{&quot;fv_title&quot;:&quot;Duplicate&quot;,&quot;sources&quot;:[{&quot;src&quot;:&quot;https://cdn.example.test/one.mp4&quot;}]}" />
        </html>
        """

        collection = parse_flowplayer_collection(page_html, "https://example.test/example-collection/")

        self.assertEqual(collection.title, "Example Collection")
        self.assertEqual(collection.slug, "example-collection")
        self.assertEqual([video.title for video in collection.videos], ["Clip One", "Clip Two"])
        self.assertEqual(collection.videos[1].source_url, "https://cdn.example.test/two.m3u8")

    @patch("videotrack.sites.flowplayer.requests.head")
    def test_resume_head_check_uses_collection_cookie(self, head) -> None:
        response = Mock()
        response.headers = {"content-length": "3"}
        head.return_value = response
        with TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "clip.mp4"
            destination.write_bytes(b"abc")
            matches = _existing_file_matches_source(
                CollectionVideo("Clip", "https://cdn.example.test/clip.mp4"),
                destination,
                "https://page.example.test/collection",
                {"session": "abc"},
            )

        self.assertTrue(matches)
        self.assertEqual(head.call_args.kwargs["headers"]["Cookie"], "session=abc")


if __name__ == "__main__":
    unittest.main()
