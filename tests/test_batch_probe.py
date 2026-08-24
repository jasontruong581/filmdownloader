"""Batch capability probing.

Two properties matter most and are asserted explicitly:

* the probe never invokes the multi-page crawler, and
* nothing it produces claims a site "supports batch download".
"""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from videotrack.core.models import BatchItem, BatchProbe
from videotrack.engines import batch

COLLECTION_HTML = """
<html><title>Example Collection</title>
<div data-item="{&quot;fv_title&quot;:&quot;One&quot;,&quot;sources&quot;:[{&quot;src&quot;:&quot;https://cdn.example.test/one.mp4&quot;}]}"></div>
<div data-item="{&quot;fv_title&quot;:&quot;Two&quot;,&quot;sources&quot;:[{&quot;src&quot;:&quot;https://cdn.example.test/two.mp4&quot;}]}"></div>
</html>
"""

LISTING_HTML = """
<html><body>
<a href="/video/one">One</a>
<a href="/video/two">Two</a>
<a href="/tag/skip">Tag</a>
</body></html>
"""

SINGLE_LINK_HTML = '<html><body><a href="/video/only">Only</a></body></html>'


def _html_response(body: str) -> Mock:
    response = Mock()
    response.text = body
    response.headers = {"Content-Type": "text/html; charset=utf-8"}
    response.raise_for_status = Mock()
    return response


def _proven(count: int = 2) -> BatchProbe:
    return BatchProbe(
        capability="playlist",
        confidence="proven",
        items=tuple(BatchItem(f"https://page.example.test/w/{i}") for i in range(count)),
        total_estimate=count,
    )


class DetectorOrderTests(unittest.TestCase):
    def test_a_playlist_short_circuits_the_cheaper_detectors_first(self) -> None:
        with (
            patch.object(batch, "_probe_ytdlp", return_value=_proven()) as ytdlp,
            patch.object(batch, "_probe_site_plugin") as plugin,
            patch.object(batch, "_probe_crawl_prefilter") as crawl,
        ):
            result = batch.probe("https://page.example.test/list")

        self.assertEqual(result.capability, "playlist")
        ytdlp.assert_called_once()
        plugin.assert_not_called()
        crawl.assert_not_called()

    def test_a_declining_ytdlp_falls_through_to_the_site_plugin(self) -> None:
        collection = BatchProbe(capability="collection", confidence="proven", items=(BatchItem("a"), BatchItem("b")))
        with (
            patch.object(batch, "_probe_ytdlp", return_value=BatchProbe(reason="nope")),
            patch.object(batch, "_probe_site_plugin", return_value=collection),
            patch.object(batch, "_probe_crawl_prefilter") as crawl,
        ):
            result = batch.probe("https://page.example.test/collection/")

        self.assertEqual(result.capability, "collection")
        crawl.assert_not_called()

    def test_a_raising_detector_is_a_decline(self) -> None:
        with (
            patch.object(batch, "_probe_ytdlp", side_effect=RuntimeError("boom")),
            patch.object(batch, "_probe_site_plugin", return_value=_proven()),
        ):
            self.assertTrue(batch.probe("https://page.example.test/x").is_batchable)


class NoEnumerationTests(unittest.TestCase):
    def test_reasons_from_each_detector_are_reported(self) -> None:
        with (
            patch.object(batch, "_probe_ytdlp", return_value=BatchProbe(reason="not a playlist")),
            patch.object(batch, "_probe_site_plugin", return_value=BatchProbe(reason="no plugin matched")),
            patch.object(batch, "_probe_crawl_prefilter", return_value=BatchProbe(reason="no child links")),
        ):
            result = batch.probe("https://page.example.test/w/1")

        self.assertFalse(result.is_batchable)
        self.assertIn("not a playlist", result.reason)
        self.assertIn("no child links", result.reason)

    def test_duplicate_reasons_are_collapsed(self) -> None:
        same = BatchProbe(reason="same reason")
        with (
            patch.object(batch, "_probe_ytdlp", return_value=same),
            patch.object(batch, "_probe_site_plugin", return_value=same),
            patch.object(batch, "_probe_crawl_prefilter", return_value=same),
        ):
            self.assertEqual(batch.probe("https://p.example.test/").reason, "same reason")

    def test_there_is_always_a_reason_to_show(self) -> None:
        empty = BatchProbe()
        with (
            patch.object(batch, "_probe_ytdlp", return_value=empty),
            patch.object(batch, "_probe_site_plugin", return_value=empty),
            patch.object(batch, "_probe_crawl_prefilter", return_value=empty),
        ):
            self.assertTrue(batch.probe("https://p.example.test/").reason)

    def test_a_single_item_is_never_batchable(self) -> None:
        one = BatchProbe(capability="playlist", confidence="proven", items=(BatchItem("a"),))

        self.assertFalse(one.is_batchable)


class SitePluginProbeTests(unittest.TestCase):
    def test_a_flowplayer_collection_is_proven_from_its_own_markup(self) -> None:
        response = _html_response(COLLECTION_HTML)
        response.url = "https://page.example.test/collection/"
        with patch("videotrack.sites.flowplayer.requests.Session") as session_cls:
            session_cls.return_value.get.return_value = response
            result = batch._probe_site_plugin("https://page.example.test/collection/", batch.BatchOptions())

        self.assertEqual(result.capability, "collection")
        self.assertEqual(result.confidence, "proven")
        self.assertEqual([item.title for item in result.items], ["One", "Two"])

    def test_a_plugin_that_raises_does_not_break_the_detector(self) -> None:
        with patch("videotrack.sites.flowplayer.requests.Session", side_effect=RuntimeError("boom")):
            result = batch._probe_site_plugin("https://page.example.test/x", batch.BatchOptions())

        self.assertFalse(result.is_batchable)


class CrawlPrefilterTests(unittest.TestCase):
    def test_matching_child_links_are_possible_not_proven(self) -> None:
        # Links on a page are not confirmed media, so confidence stops at possible.
        with patch.object(batch.requests, "get", return_value=_html_response(LISTING_HTML)):
            result = batch._probe_crawl_prefilter("https://vlxx.example.test/", batch.BatchOptions())

        self.assertEqual(result.capability, "crawl")
        self.assertEqual(result.confidence, "possible")
        self.assertEqual(len(result.items), 2)

    def test_exactly_one_request_is_made(self) -> None:
        with patch.object(batch.requests, "get", return_value=_html_response(LISTING_HTML)) as get:
            batch._probe_crawl_prefilter("https://vlxx.example.test/", batch.BatchOptions())

        self.assertEqual(get.call_count, 1)

    def test_the_multi_page_crawler_is_never_invoked(self) -> None:
        with (
            patch.object(batch.requests, "get", return_value=_html_response(LISTING_HTML)),
            patch("videotrack.crawl.crawl_site_links") as crawler,
        ):
            batch._probe_crawl_prefilter("https://vlxx.example.test/", batch.BatchOptions())

        crawler.assert_not_called()

    def test_a_host_without_crawl_rules_declines(self) -> None:
        result = batch._probe_crawl_prefilter("https://unknown.example.test/", batch.BatchOptions())

        self.assertFalse(result.is_batchable)
        self.assertIn("no site plugin claims this host", result.reason)

    def test_a_page_with_one_matching_link_declines(self) -> None:
        with patch.object(batch.requests, "get", return_value=_html_response(SINGLE_LINK_HTML)):
            result = batch._probe_crawl_prefilter("https://vlxx.example.test/", batch.BatchOptions())

        self.assertIn("fewer than two", result.reason)

    def test_a_non_html_response_declines(self) -> None:
        response = _html_response("{}")
        response.headers = {"Content-Type": "application/json"}
        with patch.object(batch.requests, "get", return_value=response):
            result = batch._probe_crawl_prefilter("https://vlxx.example.test/", batch.BatchOptions())

        self.assertIn("did not return HTML", result.reason)

    def test_a_failed_request_declines(self) -> None:
        with patch.object(batch.requests, "get", side_effect=batch.requests.RequestException("down")):
            result = batch._probe_crawl_prefilter("https://vlxx.example.test/", batch.BatchOptions())

        self.assertIn("could not be fetched", result.reason)

    def test_the_link_count_is_capped(self) -> None:
        many = "<html><body>" + "".join(f'<a href="/video/{i}">v</a>' for i in range(50)) + "</body></html>"
        options = batch.BatchOptions(crawl_limit=5)
        with patch.object(batch.requests, "get", return_value=_html_response(many)):
            result = batch._probe_crawl_prefilter("https://vlxx.example.test/", options)

        self.assertEqual(len(result.items), 5)
        self.assertTrue(result.truncated)


class SampleVerifyTests(unittest.TestCase):
    def test_only_the_requested_number_of_items_is_resolved(self) -> None:
        items = tuple(BatchItem(f"https://page.example.test/w/{i}") for i in range(10))
        with patch("videotrack.engines.chain.resolve", return_value=["ok"]) as resolve:
            verified, attempted = batch.sample_verify(items, count=2)

        self.assertEqual((verified, attempted), (2, 2))
        self.assertEqual(resolve.call_count, 2)

    def test_a_failing_item_counts_as_attempted_but_not_verified(self) -> None:
        items = (BatchItem("https://page.example.test/w/1"),)
        with patch("videotrack.engines.chain.resolve", side_effect=RuntimeError("boom")):
            self.assertEqual(batch.sample_verify(items, count=1), (0, 1))

    def test_an_unresolvable_item_is_not_verified(self) -> None:
        items = (BatchItem("https://page.example.test/w/1"),)
        with patch("videotrack.engines.chain.resolve", return_value=[]):
            self.assertEqual(batch.sample_verify(items, count=1), (0, 1))

    def test_requesting_none_resolves_nothing(self) -> None:
        items = (BatchItem("https://page.example.test/w/1"),)
        with patch("videotrack.engines.chain.resolve") as resolve:
            self.assertEqual(batch.sample_verify(items, count=0), (0, 0))

        resolve.assert_not_called()


class HonestyTests(unittest.TestCase):
    def test_no_probe_reason_claims_a_site_supports_batch_download(self) -> None:
        # A probe proves enumeration, not downloadability, and the copy must not
        # overstate that anywhere it reaches the operator.
        import inspect

        source = inspect.getsource(batch)
        lowered = source.lower()

        self.assertNotIn("supports batch", lowered.replace('report that a site "supports batch download"', ""))


if __name__ == "__main__":
    unittest.main()
