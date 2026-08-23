from __future__ import annotations

import unittest

from videotrack.static_player import extract_media_urls


class StaticPlayerExtractionTests(unittest.TestCase):
    def test_extracts_absolute_and_relative_player_urls(self) -> None:
        player_html = """
        <script>
          const config = {file: '/media/stream.m3u8'};
          const backup = 'https://cdn.example.test/video.mp4?quality=hd';
        </script>
        """

        self.assertEqual(
            extract_media_urls(player_html, "https://page.example.test/player"),
            [
                "https://cdn.example.test/video.mp4?quality=hd",
                "https://page.example.test/media/stream.m3u8",
            ],
        )

    def test_ignores_non_media_values(self) -> None:
        self.assertEqual(extract_media_urls("const file: '/assets/player.js'", "https://example.test/"), [])


if __name__ == "__main__":
    unittest.main()
