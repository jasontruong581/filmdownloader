"""Characterization tests for candidate detection.

These pin the scoring arithmetic, kind selection, rejection rules, and dedup
behavior that `detect_candidates` exhibits today, so the module can be moved and
refactored without silently changing which stream a download picks.

Offline: `probe=False` everywhere, so no HTTP request is made.
"""

from __future__ import annotations

import unittest

from videotrack.core.detect import detect_candidates
from videotrack.core.models import CaptureResult, NetworkRequest
from videotrack.hosts import DEFAULT_HOST_BONUSES


def _capture(*requests: NetworkRequest) -> CaptureResult:
    return CaptureResult(
        page_url="https://page.example.test/watch",
        final_url="https://page.example.test/watch",
        title="Example",
        user_agent="test-agent",
        cookies={},
        requests=list(requests),
    )


def _request(
    url: str,
    *,
    resource_type: str | None = None,
    content_type: str | None = None,
    status: int | None = None,
) -> NetworkRequest:
    response_headers = {"content-type": content_type} if content_type else {}
    return NetworkRequest(
        url=url,
        method="GET",
        headers={},
        resource_type=resource_type,
        status=status,
        response_headers=response_headers,
    )


class UrlPatternScoringTests(unittest.TestCase):
    def test_hls_url_scores_100(self) -> None:
        candidates = detect_candidates(_capture(_request("https://cdn.example.test/a.m3u8")), probe=False)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].kind, "hls")
        self.assertEqual(candidates[0].score, 100)
        self.assertEqual(candidates[0].source, "network_log")

    def test_dash_url_scores_90(self) -> None:
        candidates = detect_candidates(_capture(_request("https://cdn.example.test/a.mpd")), probe=False)

        self.assertEqual((candidates[0].kind, candidates[0].score), ("dash", 90))

    def test_mp4_url_scores_80(self) -> None:
        candidates = detect_candidates(_capture(_request("https://cdn.example.test/a.mp4")), probe=False)

        self.assertEqual((candidates[0].kind, candidates[0].score), ("mp4", 80))

    def test_media_resource_type_adds_five(self) -> None:
        candidates = detect_candidates(
            _capture(_request("https://cdn.example.test/a.mp4", resource_type="Media")),
            probe=False,
        )

        self.assertEqual(candidates[0].score, 85)

    def test_referer_defaults_to_capture_final_url(self) -> None:
        candidates = detect_candidates(_capture(_request("https://cdn.example.test/a.mp4")), probe=False)

        self.assertEqual(candidates[0].referer, "https://page.example.test/watch")


class ContentTypeScoringTests(unittest.TestCase):
    def test_content_type_wins_when_score_is_greater_or_equal(self) -> None:
        # URL says mp4 (80), content-type says HLS (100). Content-type wins.
        candidates = detect_candidates(
            _capture(_request("https://cdn.example.test/a.mp4", content_type="application/x-mpegURL")),
            probe=False,
        )

        self.assertEqual((candidates[0].kind, candidates[0].score), ("hls", 100))

    def test_url_kind_wins_when_content_type_scores_lower(self) -> None:
        # URL says HLS (100), content-type says mp4 (80). Kind stays HLS, score is the max.
        candidates = detect_candidates(
            _capture(_request("https://cdn.example.test/a.m3u8", content_type="video/mp4")),
            probe=False,
        )

        self.assertEqual((candidates[0].kind, candidates[0].score), ("hls", 100))


class RejectionTests(unittest.TestCase):
    def test_blocked_content_type_rejects_even_a_media_url(self) -> None:
        candidates = detect_candidates(
            _capture(_request("https://cdn.example.test/a.mp4", content_type="text/html")),
            probe=False,
        )

        self.assertEqual(candidates, [])

    def test_blocked_url_pattern_is_rejected(self) -> None:
        candidates = detect_candidates(_capture(_request("https://cdn.example.test/favicon.mp4")), probe=False)

        self.assertEqual(candidates, [])

    def test_unmatched_url_without_media_resource_type_is_skipped(self) -> None:
        candidates = detect_candidates(_capture(_request("https://cdn.example.test/script")), probe=False)

        self.assertEqual(candidates, [])

    def test_image_content_type_has_no_media_fallback(self) -> None:
        candidates = detect_candidates(
            _capture(_request("https://cdn.example.test/thumb", resource_type="Media", content_type="image/webp")),
            probe=False,
        )

        self.assertEqual(candidates, [])


class MediaFallbackTests(unittest.TestCase):
    def test_media_resource_type_without_content_type_falls_back_to_55(self) -> None:
        candidates = detect_candidates(
            _capture(_request("https://cdn.example.test/stream/segment", resource_type="Media")),
            probe=False,
        )

        # fallback 55 plus the Media bonus 5.
        self.assertEqual((candidates[0].kind, candidates[0].score), ("media", 60))

    def test_video_mp4_content_type_fallback_scores_75(self) -> None:
        candidates = detect_candidates(
            _capture(
                _request(
                    "https://cdn.example.test/stream/segment",
                    resource_type="Media",
                    content_type="video/mp4",
                )
            ),
            probe=False,
        )

        # video/mp4 is a content-type hint (mp4, 80) before the fallback table is reached.
        self.assertEqual((candidates[0].kind, candidates[0].score), ("mp4", 85))

    def test_octet_stream_fallback_scores_60(self) -> None:
        candidates = detect_candidates(
            _capture(
                _request(
                    "https://cdn.example.test/stream/segment",
                    resource_type="Media",
                    content_type="application/octet-stream",
                )
            ),
            probe=False,
        )

        self.assertEqual((candidates[0].kind, candidates[0].score), ("media", 65))


class PenaltyAndBonusTests(unittest.TestCase):
    def test_ad_path_penalty_subtracts_twenty(self) -> None:
        candidates = detect_candidates(_capture(_request("https://cdn.example.test/ads/a.mp4")), probe=False)

        self.assertEqual(candidates[0].score, 60)

    def test_preroll_penalty_subtracts_twenty(self) -> None:
        candidates = detect_candidates(_capture(_request("https://cdn.example.test/preroll-a.mp4")), probe=False)

        self.assertEqual(candidates[0].score, 60)

    def test_shipped_host_bonus_is_applied_when_passed(self) -> None:
        candidates = detect_candidates(
            _capture(_request("https://v1.tiktokcdn.com/a.mp4")),
            probe=False,
            host_bonuses=DEFAULT_HOST_BONUSES,
        )

        self.assertEqual(candidates[0].score, 98)

    def test_no_host_bonus_is_applied_by_default(self) -> None:
        # Core carries no hostnames of its own; bonuses are caller-supplied data.
        candidates = detect_candidates(_capture(_request("https://v1.tiktokcdn.com/a.mp4")), probe=False)

        self.assertEqual(candidates[0].score, 80)

    def test_host_is_derived_from_the_url(self) -> None:
        candidates = detect_candidates(_capture(_request("https://CDN.Example.Test/a.mp4")), probe=False)

        self.assertEqual(candidates[0].host, "cdn.example.test")


class DedupAndOrderingTests(unittest.TestCase):
    def test_duplicate_url_keeps_the_higher_score(self) -> None:
        candidates = detect_candidates(
            _capture(
                _request("https://cdn.example.test/a.mp4"),
                _request("https://cdn.example.test/a.mp4", resource_type="Media"),
            ),
            probe=False,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].score, 85)

    def test_duplicate_url_does_not_downgrade_an_existing_score(self) -> None:
        candidates = detect_candidates(
            _capture(
                _request("https://cdn.example.test/a.mp4", resource_type="Media"),
                _request("https://cdn.example.test/a.mp4"),
            ),
            probe=False,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].score, 85)

    def test_results_are_sorted_by_score_descending(self) -> None:
        candidates = detect_candidates(
            _capture(
                _request("https://cdn.example.test/c.mp4"),
                _request("https://cdn.example.test/a.m3u8"),
                _request("https://cdn.example.test/b.mpd"),
            ),
            probe=False,
        )

        self.assertEqual([c.score for c in candidates], [100, 90, 80])
        self.assertEqual([c.kind for c in candidates], ["hls", "dash", "mp4"])


if __name__ == "__main__":
    unittest.main()
