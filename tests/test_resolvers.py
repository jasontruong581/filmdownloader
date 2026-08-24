from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from videotrack import cli
from videotrack.core.resolvers import Resolution, ResolvedMedia, capture_from_resolution
from videotrack.static_player import StaticPlayerResolver


class ResolverContextTests(unittest.TestCase):
    def test_capture_preserves_static_session_cookies(self) -> None:
        resolution = Resolution(
            resolver="test",
            page_url="https://page.example.test/",
            final_url="https://page.example.test/final",
            title="Test",
            media=(ResolvedMedia("https://cdn.example.test/video.mp4", "https://player.example.test/", "mp4"),),
            cookies={"session": "abc"},
        )

        capture = capture_from_resolution(resolution)

        self.assertEqual(capture.cookies, {"session": "abc"})
        self.assertEqual(capture.requests[0].headers["Referer"], "https://player.example.test/")

    def test_static_player_posts_form_content_type_and_keeps_cookies(self) -> None:
        page = Mock(url="https://page.example.test/watch")
        page.text = '<title>Example</title><div data-movie="m1" data-type="10"></div>'
        player = Mock()
        player.text = "const config = {file: 'https://cdn.example.test/video.m3u8'};"
        resolver = StaticPlayerResolver()
        resolver.session = Mock()
        resolver.session.get.return_value = page
        resolver.session.post.return_value = player
        resolver.session.cookies = [SimpleNamespace(name="session", value="abc")]

        resolution = resolver.resolve("https://page.example.test/watch")

        self.assertIsNotNone(resolution)
        self.assertEqual(resolution.cookies, {"session": "abc"})
        headers = resolver.session.post.call_args.kwargs["headers"]
        self.assertEqual(headers["Content-Type"], "application/x-www-form-urlencoded; charset=UTF-8")

    @patch("videotrack.cli.capture_page")
    @patch("videotrack.cli._run_capture_pipeline")
    @patch("videotrack.cli.StaticPlayerResolver")
    def test_auto_retries_browser_after_static_failure(self, resolver_cls, run_pipeline, capture_page) -> None:
        static_resolution = Resolution(
            resolver="test",
            page_url="https://page.example.test/",
            final_url="https://page.example.test/",
            title="Test",
            media=(ResolvedMedia("https://cdn.example.test/video.mp4", "https://page.example.test/", "mp4"),),
        )
        resolver_cls.return_value.resolve.return_value = static_resolution
        browser_capture = capture_from_resolution(static_resolution)
        capture_page.return_value = browser_capture
        run_pipeline.side_effect = [3, 0]
        args = SimpleNamespace(
            autonomous=False,
            resolver="auto",
            url="https://page.example.test/",
            wait=1,
            headed=False,
        )

        result = cli.cmd_run(args)

        self.assertEqual(result, 0)
        self.assertEqual(run_pipeline.call_count, 2)
        capture_page.assert_called_once()


if __name__ == "__main__":
    unittest.main()
