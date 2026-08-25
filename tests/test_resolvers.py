from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from videotrack import cli
from videotrack.core.resolvers import Resolution, ResolvedMedia, capture_from_resolution


def _run_args(**overrides) -> SimpleNamespace:
    defaults = dict(
        autonomous=False,
        resolver="auto",
        engine=None,
        url="https://page.example.test/",
        wait=1,
        headed=False,
        cookies_from_browser="",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)
from videotrack.sites.vlxx import StaticPlayerResolver


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

    @patch("videotrack.cli._run_capture_pipeline")
    @patch("videotrack.cli.resolve_by_engine")
    def test_a_failed_download_falls_through_to_the_next_engine(self, resolve_by_engine, run_pipeline) -> None:
        # Resolution succeeding is not the same as downloading succeeding, so a
        # non-zero pipeline result must move on to the next engine.
        site = Resolution(
            resolver="vlxx",
            page_url="https://page.example.test/",
            final_url="https://page.example.test/",
            title="Static",
            media=(ResolvedMedia("https://cdn.example.test/a.mp4", "https://page.example.test/", "mp4"),),
            engine="site",
        )
        browser = Resolution(
            resolver="browser",
            page_url="https://page.example.test/",
            final_url="https://page.example.test/",
            title="Browser",
            media=(ResolvedMedia("https://cdn.example.test/b.mp4", "https://page.example.test/", "mp4"),),
            engine="browser",
        )
        resolve_by_engine.return_value = iter([("site", [site]), ("browser", [browser])])
        run_pipeline.side_effect = [3, 0]

        result = cli.cmd_run(_run_args())

        self.assertEqual(result, 0)
        self.assertEqual(run_pipeline.call_count, 2)

    @patch("videotrack.cli._run_capture_pipeline")
    @patch("videotrack.cli.resolve_by_engine")
    def test_a_successful_download_stops_the_chain(self, resolve_by_engine, run_pipeline) -> None:
        resolution = Resolution(
            resolver="ytdlp",
            page_url="https://page.example.test/",
            final_url="https://page.example.test/",
            title="Clip",
            media=(ResolvedMedia("https://cdn.example.test/a.mp4", "https://page.example.test/", "mp4"),),
            engine="ytdlp",
        )
        resolve_by_engine.return_value = iter([("ytdlp", [resolution])])
        run_pipeline.return_value = 0

        self.assertEqual(cli.cmd_run(_run_args()), 0)
        self.assertEqual(run_pipeline.call_count, 1)

    @patch("videotrack.cli._run_capture_pipeline")
    @patch("videotrack.cli.resolve_by_engine")
    def test_no_engine_resolving_reports_failure_without_downloading(self, resolve_by_engine, run_pipeline) -> None:
        resolve_by_engine.return_value = iter([])

        self.assertEqual(cli.cmd_run(_run_args()), 2)
        run_pipeline.assert_not_called()

    @patch("videotrack.cli._run_capture_pipeline")
    @patch("videotrack.cli.resolve_by_engine")
    def test_every_engine_failing_to_download_returns_the_last_code(self, resolve_by_engine, run_pipeline) -> None:
        resolution = Resolution(
            resolver="ytdlp",
            page_url="https://page.example.test/",
            final_url="https://page.example.test/",
            title="Clip",
            media=(ResolvedMedia("https://cdn.example.test/a.mp4", "https://page.example.test/", "mp4"),),
            engine="ytdlp",
        )
        resolve_by_engine.return_value = iter([("ytdlp", [resolution]), ("browser", [resolution])])
        run_pipeline.side_effect = [3, 3]

        self.assertEqual(cli.cmd_run(_run_args()), 3)


class ChainOptionFlagTests(unittest.TestCase):
    def test_the_legacy_resolver_flag_still_selects_engine_subsets(self) -> None:
        self.assertEqual(cli._chain_options(_run_args(resolver="static")).engines, ("ytdlp", "site"))
        self.assertEqual(cli._chain_options(_run_args(resolver="browser")).engines, ("browser",))
        self.assertEqual(cli._chain_options(_run_args(resolver="auto")).engines, ("ytdlp", "site", "browser"))

    def test_an_explicit_engine_flag_wins_over_the_legacy_alias(self) -> None:
        args = _run_args(resolver="browser", engine=["ytdlp"])

        self.assertEqual(cli._chain_options(args).engines, ("ytdlp",))

    def test_engine_order_is_preserved_as_given(self) -> None:
        args = _run_args(engine=["browser", "ytdlp"])

        self.assertEqual(cli._chain_options(args).engines, ("browser", "ytdlp"))

    def test_browser_wait_and_headed_reach_the_browser_engine(self) -> None:
        options = cli._chain_options(_run_args(wait=42, headed=True))

        self.assertEqual(options.browser.wait_seconds, 42)
        self.assertFalse(options.browser.headless)

    def test_browser_cookies_are_off_unless_asked_for(self) -> None:
        self.assertIsNone(cli._chain_options(_run_args()).ytdlp.cookies_from_browser)
        self.assertEqual(
            cli._chain_options(_run_args(cookies_from_browser="chrome")).ytdlp.cookies_from_browser,
            "chrome",
        )


if __name__ == "__main__":
    unittest.main()
