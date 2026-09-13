"""Finding the player document a page loaded.

Most streaming sites do not host their own video. They embed one, and the deep
scan follows those embeds when nothing direct was detected. It followed only
the long forms of the URL - `/embed/` and `/player/` - which left every site
whose host serves the short `/e/<id>` form looking like a page with no player.

Following one costs a full browser session and only the first few are tried, so
the order matters as much as the matching: a page requests its advertising
frames before its player often enough that request order is not a ranking.

No browser here: these are recorded requests.
"""

from __future__ import annotations

import unittest

from videotrack.core.detect import extract_embed_urls
from videotrack.core.models import CaptureResult, NetworkRequest

PAGE = "https://site.example.test/watch/123"


def _capture(*requests: NetworkRequest) -> CaptureResult:
    return CaptureResult(
        page_url=PAGE,
        final_url=PAGE,
        title="Clip",
        user_agent="test-agent",
        cookies={},
        requests=list(requests),
    )


def _request(url: str, resource_type: str | None = "XHR") -> NetworkRequest:
    return NetworkRequest(url=url, method="GET", headers={}, resource_type=resource_type, status=200)


class ShapeTests(unittest.TestCase):
    def test_the_long_form_is_still_matched(self) -> None:
        found = extract_embed_urls(_capture(_request("https://host.example.test/embed/abc123")))

        self.assertEqual(found, ["https://host.example.test/embed/abc123"])

    def test_a_player_path_is_matched(self) -> None:
        found = extract_embed_urls(_capture(_request("https://host.example.test/player/abc123")))

        self.assertEqual(found, ["https://host.example.test/player/abc123"])

    def test_the_short_form_is_matched(self) -> None:
        # The regression: this is the form most embed hosts actually serve, and
        # it looked like nothing at all.
        found = extract_embed_urls(_capture(_request("https://host.example.test/e/9fk2plq1")))

        self.assertEqual(found, ["https://host.example.test/e/9fk2plq1"])

    def test_an_iframe_path_is_matched(self) -> None:
        found = extract_embed_urls(_capture(_request("https://host.example.test/iframe?id=7")))

        self.assertEqual(found, ["https://host.example.test/iframe?id=7"])

    def test_a_hyphenated_embed_path_is_matched(self) -> None:
        found = extract_embed_urls(_capture(_request("https://host.example.test/embed-9fk2plq1.html")))

        self.assertEqual(found, ["https://host.example.test/embed-9fk2plq1.html"])

    def test_a_short_id_is_not_enough_to_claim_a_player(self) -> None:
        # `/e/` on its own appears in plenty of routes that are not a player,
        # and following one spends a browser session to find nothing.
        found = extract_embed_urls(_capture(_request("https://site.example.test/e/ok")))

        self.assertEqual(found, [])

    def test_an_unrelated_path_is_not_matched(self) -> None:
        found = extract_embed_urls(_capture(_request("https://site.example.test/assets/app.js")))

        self.assertEqual(found, [])

    def test_a_non_http_url_is_skipped(self) -> None:
        found = extract_embed_urls(_capture(_request("data:text/html,<iframe src='/embed/x'>")))

        self.assertEqual(found, [])


class RankingTests(unittest.TestCase):
    def test_a_document_outranks_a_request_of_another_kind(self) -> None:
        # An embed is a document loaded into a frame. A script that happens to
        # sit under the same path is not the thing worth opening a browser for.
        found = extract_embed_urls(
            _capture(
                _request("https://host.example.test/embed/script", "Script"),
                _request("https://host.example.test/e/9fk2plq1", "Document"),
            )
        )

        self.assertEqual(found[0], "https://host.example.test/e/9fk2plq1")

    def test_an_advertising_frame_ranks_below_a_player(self) -> None:
        # Pages request these first, so without the penalty the ad frame is
        # what the deep scan spends its budget on.
        found = extract_embed_urls(
            _capture(
                _request("https://ads.example.test/embed/preroll", "Document"),
                _request("https://host.example.test/embed/9fk2plq1", "Document"),
            )
        )

        self.assertEqual(found[0], "https://host.example.test/embed/9fk2plq1")

    def test_request_order_breaks_a_tie(self) -> None:
        found = extract_embed_urls(
            _capture(
                _request("https://host.example.test/embed/first", "Document"),
                _request("https://host.example.test/embed/second", "Document"),
            )
        )

        self.assertEqual(
            found,
            ["https://host.example.test/embed/first", "https://host.example.test/embed/second"],
        )

    def test_the_long_form_outranks_the_short_one_at_equal_footing(self) -> None:
        found = extract_embed_urls(
            _capture(
                _request("https://host.example.test/v/9fk2plq1abc", "Document"),
                _request("https://host.example.test/embed/9fk2plq1", "Document"),
            )
        )

        self.assertEqual(found[0], "https://host.example.test/embed/9fk2plq1")


class ExclusionTests(unittest.TestCase):
    def test_the_page_itself_is_never_returned(self) -> None:
        # It is a document request like any other, and following it re-captures
        # the page that was just captured.
        page = "https://site.example.test/embed/123"
        capture = CaptureResult(
            page_url=page,
            final_url=page,
            title="Clip",
            user_agent="test-agent",
            cookies={},
            requests=[_request(page, "Document")],
        )

        self.assertEqual(extract_embed_urls(capture), [])

    def test_analytics_is_not_a_player(self) -> None:
        found = extract_embed_urls(
            _capture(_request("https://www.googletagmanager.com/embed/gtm.js", "Document"))
        )

        self.assertEqual(found, [])

    def test_the_same_url_is_returned_once(self) -> None:
        found = extract_embed_urls(
            _capture(
                _request("https://host.example.test/embed/abc", "Document"),
                _request("https://host.example.test/embed/abc", "Document"),
            )
        )

        self.assertEqual(found, ["https://host.example.test/embed/abc"])


if __name__ == "__main__":
    unittest.main()
