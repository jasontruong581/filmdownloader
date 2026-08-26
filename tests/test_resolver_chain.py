"""Chain ordering and failure isolation, with every engine mocked."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from videotrack.core.resolvers import Resolution, ResolvedMedia
from videotrack.engines import chain


def _resolution(engine: str, title: str = "T") -> Resolution:
    return Resolution(
        resolver=engine,
        page_url="https://page.example.test/",
        final_url="https://page.example.test/",
        title=title,
        media=(ResolvedMedia("https://cdn.example.test/a.mp4", "https://page.example.test/", "mp4"),),
        engine=engine,
    )


class OrderingTests(unittest.TestCase):
    def test_the_default_order_is_ytdlp_then_site_then_browser(self) -> None:
        self.assertEqual(chain.DEFAULT_ENGINE_ORDER, ("ytdlp", "site", "browser"))

    def test_a_ytdlp_hit_short_circuits_the_rest(self) -> None:
        with (
            patch.object(chain, "_resolve_with_ytdlp", return_value=[_resolution("ytdlp")]) as ytdlp,
            patch.object(chain, "_resolve_with_sites") as site,
            patch.object(chain, "_resolve_with_browser") as browser,
        ):
            results = chain.resolve("https://page.example.test/")

        self.assertEqual([r.engine for r in results], ["ytdlp"])
        ytdlp.assert_called_once()
        site.assert_not_called()
        browser.assert_not_called()

    def test_an_empty_ytdlp_result_falls_through_to_the_site_plugin(self) -> None:
        with (
            patch.object(chain, "_resolve_with_ytdlp", return_value=[]),
            patch.object(chain, "_resolve_with_sites", return_value=[_resolution("site")]) as site,
            patch.object(chain, "_resolve_with_browser") as browser,
        ):
            results = chain.resolve("https://page.example.test/")

        self.assertEqual([r.engine for r in results], ["site"])
        site.assert_called_once()
        browser.assert_not_called()

    def test_both_static_engines_declining_falls_through_to_the_browser(self) -> None:
        with (
            patch.object(chain, "_resolve_with_ytdlp", return_value=[]),
            patch.object(chain, "_resolve_with_sites", return_value=[]),
            patch.object(chain, "_resolve_with_browser", return_value=[_resolution("browser")]) as browser,
        ):
            results = chain.resolve("https://page.example.test/")

        self.assertEqual([r.engine for r in results], ["browser"])
        browser.assert_called_once()

    def test_every_engine_declining_yields_an_empty_list(self) -> None:
        with (
            patch.object(chain, "_resolve_with_ytdlp", return_value=[]),
            patch.object(chain, "_resolve_with_sites", return_value=[]),
            patch.object(chain, "_resolve_with_browser", return_value=[]),
        ):
            self.assertEqual(chain.resolve("https://page.example.test/"), [])

    def test_a_restricted_engine_list_skips_the_others(self) -> None:
        options = chain.ChainOptions(engines=("browser",))
        with (
            patch.object(chain, "_resolve_with_ytdlp") as ytdlp,
            patch.object(chain, "_resolve_with_browser", return_value=[_resolution("browser")]),
        ):
            results = chain.resolve("https://page.example.test/", options)

        ytdlp.assert_not_called()
        self.assertEqual(len(results), 1)

    def test_an_unknown_engine_name_is_skipped_rather_than_fatal(self) -> None:
        options = chain.ChainOptions(engines=("nonsense", "ytdlp"))
        with patch.object(chain, "_resolve_with_ytdlp", return_value=[_resolution("ytdlp")]):
            results = chain.resolve("https://page.example.test/", options)

        self.assertEqual(len(results), 1)


class FailureIsolationTests(unittest.TestCase):
    def test_a_raising_engine_does_not_stop_the_chain(self) -> None:
        with (
            patch.object(chain, "_resolve_with_ytdlp", side_effect=RuntimeError("boom")),
            patch.object(chain, "_resolve_with_sites", return_value=[_resolution("site")]),
        ):
            results = chain.resolve("https://page.example.test/")

        self.assertEqual([r.engine for r in results], ["site"])

    def test_all_engines_raising_yields_an_empty_list(self) -> None:
        with (
            patch.object(chain, "_resolve_with_ytdlp", side_effect=RuntimeError("boom")),
            patch.object(chain, "_resolve_with_sites", side_effect=RuntimeError("boom")),
            patch.object(chain, "_resolve_with_browser", side_effect=RuntimeError("boom")),
        ):
            self.assertEqual(chain.resolve("https://page.example.test/"), [])


class FailureVisibilityTests(unittest.TestCase):
    """A broken engine must not stop the chain, and must not be silent either.

    At debug level a failing engine was indistinguishable from a page no engine
    supports. Those are different problems with different fixes, and telling
    them apart from a server log is the whole point.
    """

    def test_a_raising_engine_is_logged_at_warning(self) -> None:
        def exploding(url, options):
            raise RuntimeError("chromedriver went missing")

        with patch.object(chain, "_runner_for", return_value=exploding):
            with self.assertLogs("videotrack.engines.chain", level="WARNING") as captured:
                results = chain.resolve("https://page.example.test/x")

        self.assertEqual(results, [])
        joined = "\n".join(captured.output)
        self.assertIn("chromedriver went missing", joined)
        self.assertIn("RuntimeError", joined)

    def test_an_unknown_engine_name_is_logged_at_warning(self) -> None:
        # A typo in the engine setting otherwise does nothing at all, visibly.
        with patch.object(chain, "_runner_for", return_value=None):
            with self.assertLogs("videotrack.engines.chain", level="WARNING") as captured:
                chain.resolve("https://page.example.test/x", chain.ChainOptions(engines=("nope",)))

        self.assertIn("nope", "\n".join(captured.output))

    def test_a_later_engine_still_runs_after_an_earlier_one_fails(self) -> None:
        def runner_for(name: str):
            if name == "ytdlp":
                def exploding(url, options):
                    raise RuntimeError("extractor blew up")

                return exploding

            def working(url, options):
                return [_resolution(name)]

            return working

        with patch.object(chain, "_runner_for", side_effect=runner_for):
            with self.assertLogs("videotrack.engines.chain", level="WARNING"):
                results = chain.resolve("https://page.example.test/x")

        self.assertEqual([item.engine for item in results], ["site"])


class SiteEngineTests(unittest.TestCase):
    def test_a_raising_site_resolver_is_skipped_for_the_next_one(self) -> None:
        class Broken:
            name = "broken"

            def resolve(self, url):
                raise RuntimeError("boom")

        class Working:
            name = "working"

            def resolve(self, url):
                return _resolution("working")

        with patch("videotrack.sites.iter_resolvers", return_value=(Broken(), Working())):
            results = chain._resolve_with_sites("https://page.example.test/", chain.ChainOptions())

        self.assertEqual(len(results), 1)

    def test_site_results_are_labelled_with_the_site_engine(self) -> None:
        class Working:
            name = "working"

            def resolve(self, url):
                return _resolution("vlxx")

        with patch("videotrack.sites.iter_resolvers", return_value=(Working(),)):
            results = chain._resolve_with_sites("https://page.example.test/", chain.ChainOptions())

        self.assertEqual(results[0].engine, "site")
        # The originating resolver name is still recorded.
        self.assertEqual(results[0].resolver, "vlxx")

    def test_a_resolver_exposing_resolve_many_is_used_for_multiple_items(self) -> None:
        class Multi:
            name = "multi"

            def resolve_many(self, url):
                return [_resolution("multi", "a"), _resolution("multi", "b")]

            def resolve(self, url):
                raise AssertionError("resolve_many should be preferred")

        with patch("videotrack.sites.iter_resolvers", return_value=(Multi(),)):
            results = chain._resolve_with_sites("https://page.example.test/", chain.ChainOptions())

        self.assertEqual([r.title for r in results], ["a", "b"])

    def test_a_resolver_returning_none_declines(self) -> None:
        class Declines:
            name = "declines"

            def resolve(self, url):
                return None

        with patch("videotrack.sites.iter_resolvers", return_value=(Declines(),)):
            self.assertEqual(chain._resolve_with_sites("https://p.example.test/", chain.ChainOptions()), [])


class LegacyAliasTests(unittest.TestCase):
    def test_the_retired_resolver_flag_maps_onto_engine_subsets(self) -> None:
        self.assertEqual(chain.RESOLVER_ALIASES["auto"], chain.DEFAULT_ENGINE_ORDER)
        self.assertEqual(chain.RESOLVER_ALIASES["static"], ("ytdlp", "site"))
        self.assertEqual(chain.RESOLVER_ALIASES["browser"], ("browser",))

    def test_static_never_reaches_the_browser(self) -> None:
        self.assertNotIn("browser", chain.RESOLVER_ALIASES["static"])


class YtdlpAvailabilityTests(unittest.TestCase):
    def test_an_absent_ytdlp_is_a_decline_not_a_failure(self) -> None:
        with patch("videotrack.engines.ytdlp_resolver._ytdlp_module", return_value=None):
            self.assertEqual(chain._resolve_with_ytdlp("https://p.example.test/", chain.ChainOptions()), [])


if __name__ == "__main__":
    unittest.main()
