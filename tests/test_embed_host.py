"""The pieces a player-host plugin is built from.

Most streaming sites embed one of a small number of player hosts, so the host
is the unit worth supporting: a plugin for one reaches every site that embeds
it. All of this already existed inside the `vlxx` plugin, where no second host
could reach it.

No network here: the session is a dictionary of pages.
"""

from __future__ import annotations

import unittest

import requests

from videotrack.sites import embed

PLAYER = "https://host.example.test/e/9fk2plq1"


class _Page:
    def __init__(self, markup: str) -> None:
        self.text = markup

    def raise_for_status(self) -> None:
        return None


class _Site:
    """A session that serves a fixed set of pages and records what was asked."""

    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.requested: list[str] = []
        self.headers: dict[str, str] = {}
        self.cookies: tuple = ()

    def update(self, _values) -> None:  # pragma: no cover - session.headers.update
        return None

    def get(self, url, headers=None, timeout=None):
        self.requested.append(url)
        if url not in self.pages:
            raise requests.RequestException(f"no such page: {url}")
        return _Page(self.pages[url])


def _resolver(pages: dict[str, str]) -> tuple[embed.EmbedResolver, _Site]:
    resolver = embed.EmbedResolver(name="test-host")
    site = _Site(pages)
    resolver.session = site  # type: ignore[assignment]
    return resolver, site


class MarkupReadingTests(unittest.TestCase):
    def test_a_full_media_url_is_found(self) -> None:
        found = embed.extract_media_urls(
            '<script>var s = "https://cdn.example.test/a/index.m3u8?t=1";</script>', PLAYER
        )

        self.assertEqual(found, ["https://cdn.example.test/a/index.m3u8?t=1"])

    def test_a_jw_declaration_is_resolved_against_the_player(self) -> None:
        found = embed.extract_media_urls("sources: [{ file: '/stream/a.m3u8' }]", PLAYER)

        self.assertEqual(found, ["https://host.example.test/stream/a.m3u8"])

    def test_a_declaration_that_is_not_media_is_ignored(self) -> None:
        self.assertEqual(embed.extract_media_urls("src: '/assets/player.js'", PLAYER), [])

    def test_frames_are_resolved_against_the_page_that_declared_them(self) -> None:
        found = embed.extract_iframe_urls('<iframe src="//other.example.test/e/x1y2z3"></iframe>', PLAYER)

        self.assertEqual(found, ["https://other.example.test/e/x1y2z3"])

    def test_the_title_is_read_and_cleaned(self) -> None:
        self.assertEqual(embed.page_title("<title>  Clip &amp; more\n</title>"), "Clip & more")

    def test_a_page_without_a_title_yields_empty(self) -> None:
        self.assertEqual(embed.page_title("<html></html>"), "")


class ResolverTests(unittest.TestCase):
    def test_media_declared_on_the_player_page_is_returned(self) -> None:
        resolver, _ = _resolver(
            {PLAYER: '<title>Clip</title><script>file: "https://cdn.example.test/a.m3u8"</script>'}
        )

        resolution = resolver.resolve(PLAYER)

        assert resolution is not None
        self.assertEqual([item.url for item in resolution.media], ["https://cdn.example.test/a.m3u8"])
        self.assertEqual(resolution.title, "Clip")

    def test_the_media_kind_is_recorded(self) -> None:
        resolver, _ = _resolver({PLAYER: '<script>file: "https://cdn.example.test/a.m3u8"</script>'})

        resolution = resolver.resolve(PLAYER)

        assert resolution is not None
        self.assertEqual(resolution.media[0].kind, "hls")

    def test_one_nested_frame_is_followed(self) -> None:
        # The common shape: the page the site embeds is a wrapper around the
        # page that actually declares the media.
        inner = "https://inner.example.test/e/abcd1234"
        resolver, site = _resolver(
            {
                PLAYER: f'<iframe src="{inner}"></iframe>',
                inner: '<script>file: "https://cdn.example.test/a.m3u8"</script>',
            }
        )

        resolution = resolver.resolve(PLAYER)

        assert resolution is not None
        self.assertEqual(resolution.media[0].url, "https://cdn.example.test/a.m3u8")
        self.assertIn(inner, site.requested)

    def test_the_referer_for_nested_media_is_the_page_that_declared_it(self) -> None:
        # A player host that checks it rejects the outer page's URL.
        inner = "https://inner.example.test/e/abcd1234"
        resolver, _ = _resolver(
            {
                PLAYER: f'<iframe src="{inner}"></iframe>',
                inner: '<script>file: "https://cdn.example.test/a.m3u8"</script>',
            }
        )

        resolution = resolver.resolve(PLAYER)

        assert resolution is not None
        self.assertEqual(resolution.media[0].referer, inner)

    def test_two_hops_are_followed(self) -> None:
        first = "https://one.example.test/e/abcd1234"
        second = "https://two.example.test/e/efgh5678"
        resolver, _ = _resolver(
            {
                PLAYER: f'<iframe src="{first}"></iframe>',
                first: f'<iframe src="{second}"></iframe>',
                second: '<script>file: "https://cdn.example.test/a.m3u8"</script>',
            }
        )

        self.assertIsNotNone(resolver.resolve(PLAYER))

    def test_following_stops_at_the_hop_limit(self) -> None:
        # A page that keeps pointing at another page is a redirect loop rather
        # than a player, and following it is a request storm, not a download.
        third = "https://three.example.test/e/ijkl9012"
        resolver, site = _resolver(
            {
                PLAYER: '<iframe src="https://one.example.test/e/abcd1234"></iframe>',
                "https://one.example.test/e/abcd1234": '<iframe src="https://two.example.test/e/efgh5678"></iframe>',
                "https://two.example.test/e/efgh5678": f'<iframe src="{third}"></iframe>',
                third: '<script>file: "https://cdn.example.test/a.m3u8"</script>',
            }
        )

        self.assertIsNone(resolver.resolve(PLAYER))
        self.assertNotIn(third, site.requested)

    def test_a_frame_cycle_is_not_followed_twice(self) -> None:
        other = "https://other.example.test/e/abcd1234"
        resolver, site = _resolver(
            {
                PLAYER: f'<iframe src="{other}"></iframe>',
                other: f'<iframe src="{PLAYER}"></iframe>',
            }
        )

        self.assertIsNone(resolver.resolve(PLAYER))
        self.assertEqual(site.requested.count(PLAYER), 1)

    def test_a_page_with_no_media_resolves_to_nothing(self) -> None:
        resolver, _ = _resolver({PLAYER: "<html><body>no player here</body></html>"})

        self.assertIsNone(resolver.resolve(PLAYER))

    def test_a_page_that_cannot_be_fetched_resolves_to_nothing(self) -> None:
        # A host that refuses the request is a page this resolver does not
        # recognise, which the chain answers by trying the next engine.
        resolver, _ = _resolver({})

        self.assertIsNone(resolver.resolve(PLAYER))


class _TestHost(embed.EmbedHostPlugin):
    name = "test-host"
    hosts = ("host.example.test", "mirror.example.test")


class HostPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plugin = _TestHost()

    def test_a_declared_host_is_claimed(self) -> None:
        self.assertTrue(self.plugin.handles("https://host.example.test/e/9fk2plq1"))

    def test_a_subdomain_is_claimed_too(self) -> None:
        # These hosts rotate subdomains freely.
        self.assertTrue(self.plugin.handles("https://cdn7.host.example.test/e/9fk2plq1"))

    def test_a_second_declared_host_is_claimed(self) -> None:
        self.assertTrue(self.plugin.handles("https://mirror.example.test/e/9fk2plq1"))

    def test_an_unrelated_host_is_not_claimed(self) -> None:
        self.assertFalse(self.plugin.handles("https://other.example.test/e/9fk2plq1"))

    def test_a_host_that_merely_ends_with_the_name_is_not_claimed(self) -> None:
        # `evilhost.example.test` is not a subdomain of `host.example.test`.
        self.assertFalse(self.plugin.handles("https://evilhost.example.test/e/9fk2plq1"))

    def test_a_url_with_no_host_is_not_claimed(self) -> None:
        self.assertFalse(self.plugin.handles("not a url"))

    def test_the_prefilter_performs_no_io(self) -> None:
        # The registry calls this for every plugin on every resolve; a request
        # here would cost one per plugin per page.
        def explode(*args, **kwargs):
            raise AssertionError("handles() must not perform I/O")

        original = requests.Session.get
        requests.Session.get = explode  # type: ignore[method-assign]
        try:
            self.plugin.handles("https://host.example.test/e/9fk2plq1")
        finally:
            requests.Session.get = original  # type: ignore[method-assign]

    def test_the_plugin_supplies_a_resolver_named_after_it(self) -> None:
        resolver = self.plugin.resolver()

        self.assertIsInstance(resolver, embed.EmbedResolver)
        self.assertEqual(resolver.name, "test-host")


if __name__ == "__main__":
    unittest.main()
