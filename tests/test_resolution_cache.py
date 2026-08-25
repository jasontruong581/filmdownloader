"""The resolve-to-queue handoff.

Queueing must not re-resolve. For yt-dlp that would be a wasted extract; for the
browser engine it is a second full Chrome session per job. But captured media
URLs carry time-limited tokens, so the cache is short-lived by design.
"""

from __future__ import annotations

import unittest

from videotrack.core.resolvers import Resolution, ResolvedMedia
from videotrack.jobs.cache import ResolutionCache


def _resolution(title: str = "Clip") -> Resolution:
    return Resolution(
        resolver="ytdlp",
        page_url="https://page.example.test/w/1",
        final_url="https://page.example.test/w/1",
        title=title,
        media=(ResolvedMedia("https://cdn.example.test/a.mp4", "https://page.example.test/w/1", "mp4"),),
        engine="ytdlp",
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class HitTests(unittest.TestCase):
    def test_a_stored_resolution_is_returned_by_its_id(self) -> None:
        cache = ResolutionCache()
        resolution = _resolution()

        resolution_id = cache.put(resolution)

        self.assertIs(cache.get(resolution_id), resolution)

    def test_each_put_gets_its_own_id(self) -> None:
        cache = ResolutionCache()

        first = cache.put(_resolution("a"))
        second = cache.put(_resolution("b"))

        self.assertNotEqual(first, second)
        self.assertEqual(cache.get(first).title, "a")
        self.assertEqual(cache.get(second).title, "b")

    def test_an_unknown_id_misses(self) -> None:
        self.assertIsNone(ResolutionCache().get("nope"))

    def test_a_none_id_misses_without_raising(self) -> None:
        # A job queued from a bare URL carries no resolution id.
        self.assertIsNone(ResolutionCache().get(None))

    def test_an_empty_id_misses(self) -> None:
        self.assertIsNone(ResolutionCache().get(""))


class ExpiryTests(unittest.TestCase):
    def test_an_entry_expires_after_its_ttl(self) -> None:
        clock = _Clock()
        cache = ResolutionCache(ttl_seconds=60.0, clock=clock)
        resolution_id = cache.put(_resolution())

        clock.advance(61.0)

        self.assertIsNone(cache.get(resolution_id))

    def test_an_entry_survives_within_its_ttl(self) -> None:
        clock = _Clock()
        cache = ResolutionCache(ttl_seconds=60.0, clock=clock)
        resolution_id = cache.put(_resolution())

        clock.advance(59.0)

        self.assertIsNotNone(cache.get(resolution_id))

    def test_an_expired_entry_is_dropped_from_the_cache(self) -> None:
        clock = _Clock()
        cache = ResolutionCache(ttl_seconds=60.0, clock=clock)
        cache.put(_resolution())
        clock.advance(61.0)

        cache.put(_resolution())

        self.assertEqual(len(cache), 1)


class EvictionTests(unittest.TestCase):
    def test_the_cache_is_bounded(self) -> None:
        cache = ResolutionCache(max_entries=4)

        for index in range(20):
            cache.put(_resolution(str(index)))

        self.assertLessEqual(len(cache), 4)

    def test_discarding_removes_an_entry(self) -> None:
        cache = ResolutionCache()
        resolution_id = cache.put(_resolution())

        cache.discard(resolution_id)

        self.assertIsNone(cache.get(resolution_id))

    def test_discarding_an_unknown_id_is_harmless(self) -> None:
        cache = ResolutionCache()

        cache.discard("nope")
        cache.discard(None)

        self.assertEqual(len(cache), 0)


class BrowserCaptureReuseTests(unittest.TestCase):
    def test_a_cached_browser_resolution_keeps_its_capture(self) -> None:
        # This is the expensive case the cache exists for: without it, queueing
        # a browser-resolved URL pays a second Chrome session.
        from videotrack.core.models import CaptureResult

        capture = CaptureResult(
            page_url="https://page.example.test/w/1",
            final_url="https://page.example.test/w/1",
            title="Clip",
            user_agent="test-agent",
            cookies={"session": "abc"},
            requests=[],
        )
        resolution = Resolution(
            resolver="browser",
            page_url="https://page.example.test/w/1",
            final_url="https://page.example.test/w/1",
            title="Clip",
            media=(),
            engine="browser",
            capture=capture,
        )
        cache = ResolutionCache()

        resolution_id = cache.put(resolution)

        self.assertIs(cache.get(resolution_id).capture, capture)
        self.assertEqual(cache.get(resolution_id).capture.cookies, {"session": "abc"})


if __name__ == "__main__":
    unittest.main()
