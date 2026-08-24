"""The site registry is what keeps core free of site knowledge.

These tests cover routing (URL prefilter, candidate kind, crawl preset), the
guarantee that a broken plugin cannot break resolution, and the bundled plugins'
registration.
"""

from __future__ import annotations

import unittest
from pathlib import Path

from videotrack import sites
from videotrack.core.models import CaptureResult, CrawlPreset, PageMetadata, StreamCandidate


def _capture(url: str = "https://page.example.test/watch") -> CaptureResult:
    return CaptureResult(
        page_url=url,
        final_url=url,
        title="Example",
        user_agent="test-agent",
        cookies={},
        requests=[],
    )


class _AlphaPlugin(sites.BaseSitePlugin):
    name = "test-alpha"

    def handles(self, url: str) -> bool:
        return "alpha.example.test" in url

    def claims_kind(self, kind: str) -> bool:
        return kind == "alpha_asset"

    def metadata(self, capture, page_html=None):
        return PageMetadata(title="from-alpha")

    def crawl_preset(self) -> CrawlPreset:
        return CrawlPreset(name="test-alpha", include_substring="/a/")

    def postprocess(self, capture, candidate, out_file):
        return Path("converted-by-alpha.mp4")

    def output_base(self, candidate, capture, base):
        return "alpha-base"


class _BetaPlugin(sites.BaseSitePlugin):
    name = "test-beta"

    def handles(self, url: str) -> bool:
        return "beta.example.test" in url


class _BrokenPlugin(sites.BaseSitePlugin):
    name = "test-broken"

    def handles(self, url: str) -> bool:
        raise RuntimeError("this plugin is broken")

    def claims_kind(self, kind: str) -> bool:
        raise RuntimeError("this plugin is broken")

    def crawl_preset(self):
        raise RuntimeError("this plugin is broken")


class RegistryRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(sites.unregister, "test-alpha")
        self.addCleanup(sites.unregister, "test-beta")
        self.addCleanup(sites.unregister, "test-broken")
        sites.register(_AlphaPlugin())
        sites.register(_BetaPlugin())

    def test_each_url_routes_to_its_own_plugin(self) -> None:
        self.assertEqual(sites.plugin_for("https://alpha.example.test/a/1").name, "test-alpha")
        self.assertEqual(sites.plugin_for("https://beta.example.test/b/1").name, "test-beta")

    def test_an_unclaimed_url_routes_nowhere(self) -> None:
        self.assertIsNone(sites.plugin_for("https://unknown.example.test/x"))

    def test_candidate_kind_routes_postprocessing(self) -> None:
        self.assertEqual(sites.plugin_for_kind("alpha_asset").name, "test-alpha")
        self.assertIsNone(sites.plugin_for_kind("mp4"))

    def test_registering_the_same_name_replaces_rather_than_duplicates(self) -> None:
        before = len(sites.registered())

        sites.register(_AlphaPlugin())

        self.assertEqual(len(sites.registered()), before)

    def test_crawl_preset_is_resolved_through_the_claiming_plugin(self) -> None:
        preset = sites.crawl_preset_for("https://alpha.example.test/a/1")

        self.assertIsNotNone(preset)
        self.assertEqual(preset.include_substring, "/a/")

    def test_a_plugin_without_a_preset_is_not_offered_as_a_choice(self) -> None:
        self.assertIn("test-alpha", sites.crawl_preset_names())
        self.assertNotIn("test-beta", sites.crawl_preset_names())

    def test_preset_lookup_by_name(self) -> None:
        self.assertEqual(sites.crawl_preset_named("test-alpha").name, "test-alpha")
        self.assertIsNone(sites.crawl_preset_named("test-beta"))
        self.assertIsNone(sites.crawl_preset_named("does-not-exist"))


class BrokenPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(sites.unregister, "test-broken")
        self.addCleanup(sites.unregister, "test-alpha")
        sites.register(_BrokenPlugin())
        sites.register(_AlphaPlugin())

    def test_a_raising_prefilter_does_not_stop_routing(self) -> None:
        self.assertEqual(sites.plugin_for("https://alpha.example.test/a/1").name, "test-alpha")

    def test_a_raising_kind_check_does_not_stop_routing(self) -> None:
        self.assertEqual(sites.plugin_for_kind("alpha_asset").name, "test-alpha")

    def test_a_raising_preset_hook_is_skipped(self) -> None:
        self.assertNotIn("test-broken", sites.crawl_preset_names())


class BundledPluginTests(unittest.TestCase):
    def test_the_bundled_plugins_are_registered_in_a_stable_order(self) -> None:
        names = sites.plugin_names()

        self.assertEqual(names[:3], ("vlxx", "quatvn", "flowplayer"))

    def test_the_quatvn_plugin_claims_its_asset_kind(self) -> None:
        self.assertEqual(sites.plugin_for_kind("quatvn_webp").name, "quatvn")

    def test_the_flowplayer_plugin_claims_no_url(self) -> None:
        # Its pattern is markup, not hostname, and a prefilter must not fetch.
        self.assertIsNone(sites.plugin_for("https://anything.example.test/collection/"))

    def test_the_vlxx_plugin_exposes_a_resolver(self) -> None:
        plugin = sites.plugin_for("https://vlxx.example.test/video/1")

        self.assertEqual(plugin.name, "vlxx")
        self.assertIsNotNone(plugin.resolver())

    def test_iter_resolvers_returns_only_plugins_that_have_one(self) -> None:
        self.assertGreaterEqual(len(sites.iter_resolvers()), 1)


class QuatvnNamingTests(unittest.TestCase):
    def test_asset_kind_gets_a_per_clip_suffix(self) -> None:
        plugin = sites.plugin_for_kind("quatvn_webp")
        candidate = StreamCandidate(
            url="https://cdn.example.test/stream/name%20(3).webp",
            kind="quatvn_webp",
            score=80,
            source="test",
        )

        self.assertEqual(plugin.output_base(candidate, _capture(), "base"), "base-clip-03")

    def test_a_foreign_kind_is_left_alone(self) -> None:
        plugin = sites.plugin_for_kind("quatvn_webp")
        candidate = StreamCandidate(url="https://cdn.example.test/a.mp4", kind="mp4", score=80, source="test")

        self.assertIsNone(plugin.output_base(candidate, _capture(), "base"))


if __name__ == "__main__":
    unittest.main()
