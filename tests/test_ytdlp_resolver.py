"""yt-dlp info-dict mapping, driven by fixtures. No network, no extraction.

The point of these is that a yt-dlp release renaming or dropping a field should
degrade a label, never raise, because every read goes through one mapper.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from videotrack.engines import ytdlp_resolver as yr

SINGLE_VIDEO = {
    "_type": "video",
    "title": "  Example Clip  ",
    "webpage_url": "https://page.example.test/w/1",
    "duration": 128.4,
    "thumbnail": "https://img.example.test/t.jpg",
    "uploader": "Someone",
    "formats": [
        {
            "format_id": "140",
            "ext": "m4a",
            "vcodec": "none",
            "acodec": "mp4a.40.2",
            "tbr": 128.0,
            "url": "https://cdn.example.test/a.m4a",
        },
        {
            "format_id": "137",
            "ext": "mp4",
            "height": 1080,
            "width": 1920,
            "vcodec": "avc1",
            "acodec": "none",
            "tbr": 2400.0,
            "filesize": 412_000_000,
            "url": "https://cdn.example.test/v.mp4",
        },
        {
            "format_id": "22",
            "ext": "mp4",
            "height": 720,
            "vcodec": "avc1",
            "acodec": "mp4a.40.2",
            "tbr": 1200.0,
            "url": "https://cdn.example.test/b.mp4",
        },
    ],
}

PLAYLIST = {
    "_type": "playlist",
    "title": "Example Playlist",
    "playlist_count": 3,
    "entries": [
        {"title": "One", "webpage_url": "https://page.example.test/w/1"},
        {"title": "Two", "url": "https://page.example.test/w/2"},
        {"title": "Three", "webpage_url": "https://page.example.test/w/3"},
    ],
}


class FormatMappingTests(unittest.TestCase):
    def test_formats_are_sorted_best_first(self) -> None:
        formats = yr.map_formats(SINGLE_VIDEO)

        self.assertEqual([f.format_id for f in formats], ["22", "137", "140"])

    def test_tracks_are_classified_from_the_codecs(self) -> None:
        by_id = {f.format_id: f for f in yr.map_formats(SINGLE_VIDEO)}

        self.assertEqual(by_id["22"].track, "both")
        self.assertEqual(by_id["137"].track, "video-only")
        self.assertEqual(by_id["140"].track, "audio-only")

    def test_labels_only_state_known_facts(self) -> None:
        by_id = {f.format_id: f for f in yr.map_formats(SINGLE_VIDEO)}

        self.assertEqual(by_id["137"].label(), "1080p / mp4 / 2.4 Mbps / 392.9 MB / video only")
        self.assertEqual(by_id["22"].label(), "720p / mp4 / 1.2 Mbps")

    def test_audio_bitrate_is_labelled_in_kbps(self) -> None:
        by_id = {f.format_id: f for f in yr.map_formats(SINGLE_VIDEO)}

        self.assertIn("128 kbps", by_id["140"].label())

    def test_a_format_without_an_id_is_dropped(self) -> None:
        formats = yr.map_formats({"formats": [{"ext": "mp4", "url": "https://cdn.example.test/x.mp4"}]})

        self.assertEqual(formats, ())

    def test_a_format_without_a_url_is_dropped(self) -> None:
        formats = yr.map_formats({"formats": [{"format_id": "1", "ext": "mp4"}]})

        self.assertEqual(formats, ())

    def test_non_numeric_values_degrade_instead_of_raising(self) -> None:
        formats = yr.map_formats(
            {
                "formats": [
                    {
                        "format_id": "1",
                        "url": "https://cdn.example.test/x.mp4",
                        "height": "not-a-number",
                        "tbr": "nope",
                        "filesize": {},
                    }
                ]
            }
        )

        self.assertEqual(len(formats), 1)
        self.assertIsNone(formats[0].height)
        self.assertIsNone(formats[0].tbr)
        self.assertEqual(formats[0].label(), "unknown")

    def test_a_missing_formats_key_yields_nothing(self) -> None:
        self.assertEqual(yr.map_formats({"title": "x"}), ())

    def test_non_dict_entries_in_formats_are_skipped(self) -> None:
        formats = yr.map_formats({"formats": ["garbage", None, {"format_id": "1", "url": "https://c.example.test/a.mp4"}]})

        self.assertEqual(len(formats), 1)


class ResolutionMappingTests(unittest.TestCase):
    def test_metadata_is_carried_so_naming_needs_no_scraping(self) -> None:
        resolution = yr.resolution_from_info(SINGLE_VIDEO, "https://page.example.test/w/1", yr.YtDlpOptions())

        self.assertEqual(resolution.title, "Example Clip")
        self.assertEqual(resolution.duration, 128.4)
        self.assertEqual(resolution.thumbnail, "https://img.example.test/t.jpg")
        self.assertEqual(resolution.uploader, "Someone")
        self.assertEqual(resolution.engine, "ytdlp")

    def test_every_format_url_becomes_a_media_entry(self) -> None:
        resolution = yr.resolution_from_info(SINGLE_VIDEO, "https://page.example.test/w/1", yr.YtDlpOptions())

        self.assertEqual(len(resolution.media), 3)
        self.assertEqual(resolution.media[0].referer, "https://page.example.test/w/1")

    def test_a_top_level_url_is_preferred_first(self) -> None:
        info = {"url": "https://cdn.example.test/direct.mp4", "formats": SINGLE_VIDEO["formats"]}

        resolution = yr.resolution_from_info(info, "https://page.example.test/w/1", yr.YtDlpOptions())

        self.assertEqual(resolution.media[0].url, "https://cdn.example.test/direct.mp4")

    def test_info_with_no_usable_media_returns_none(self) -> None:
        self.assertIsNone(yr.resolution_from_info({"title": "x"}, "https://p.example.test/", yr.YtDlpOptions()))


class ResolveManyTests(unittest.TestCase):
    def test_a_single_video_yields_one_resolution(self) -> None:
        resolver = yr.YtDlpResolver()
        with patch.object(resolver, "_extract", return_value=SINGLE_VIDEO):
            results = resolver.resolve_many("https://page.example.test/w/1")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Example Clip")

    def test_a_declining_extraction_yields_an_empty_list(self) -> None:
        resolver = yr.YtDlpResolver()
        with patch.object(resolver, "_extract", return_value=None):
            self.assertEqual(resolver.resolve_many("https://page.example.test/w/1"), [])

    def test_resolve_returns_the_first_item_or_none(self) -> None:
        resolver = yr.YtDlpResolver()
        with patch.object(resolver, "_extract", return_value=SINGLE_VIDEO):
            self.assertIsNotNone(resolver.resolve("https://page.example.test/w/1"))
        with patch.object(resolver, "_extract", return_value=None):
            self.assertIsNone(resolver.resolve("https://page.example.test/w/1"))

    def test_an_extraction_error_is_a_decline_not_an_exception(self) -> None:
        resolver = yr.YtDlpResolver()

        class Boom:
            def __enter__(self):
                raise RuntimeError("unsupported URL")

            def __exit__(self, *exc):
                return False

        fake_module = type("M", (), {"YoutubeDL": lambda *a, **k: Boom()})
        with patch.object(yr, "_ytdlp_module", return_value=fake_module):
            self.assertEqual(resolver.resolve_many("https://page.example.test/w/1"), [])

    def test_an_absent_ytdlp_declines(self) -> None:
        resolver = yr.YtDlpResolver()
        with patch.object(yr, "_ytdlp_module", return_value=None):
            self.assertFalse(resolver.available)
            self.assertEqual(resolver.resolve_many("https://page.example.test/w/1"), [])


class BatchProbeTests(unittest.TestCase):
    def test_a_playlist_is_proven_with_its_entries_listed(self) -> None:
        resolver = yr.YtDlpResolver()
        with patch.object(resolver, "_extract", return_value=PLAYLIST):
            probe = resolver.probe_batch("https://page.example.test/list")

        self.assertEqual(probe.capability, "playlist")
        self.assertEqual(probe.confidence, "proven")
        self.assertTrue(probe.is_batchable)
        self.assertEqual([item.title for item in probe.items], ["One", "Two", "Three"])
        self.assertEqual(probe.total_estimate, 3)
        self.assertFalse(probe.truncated)

    def test_a_single_video_is_not_batchable_and_says_why(self) -> None:
        resolver = yr.YtDlpResolver()
        with patch.object(resolver, "_extract", return_value=SINGLE_VIDEO):
            probe = resolver.probe_batch("https://page.example.test/w/1")

        self.assertFalse(probe.is_batchable)
        self.assertIn("single video", probe.reason)

    def test_an_unrecognized_url_says_so(self) -> None:
        resolver = yr.YtDlpResolver()
        with patch.object(resolver, "_extract", return_value=None):
            probe = resolver.probe_batch("https://page.example.test/x")

        self.assertFalse(probe.is_batchable)
        self.assertIn("does not recognize", probe.reason)

    def test_a_one_entry_playlist_is_not_batchable(self) -> None:
        resolver = yr.YtDlpResolver()
        one = {"_type": "playlist", "entries": [{"webpage_url": "https://page.example.test/w/1"}]}
        with patch.object(resolver, "_extract", return_value=one):
            probe = resolver.probe_batch("https://page.example.test/list")

        self.assertFalse(probe.is_batchable)
        self.assertIn("fewer than two", probe.reason)

    def test_entries_without_a_url_are_skipped(self) -> None:
        resolver = yr.YtDlpResolver()
        mixed = {
            "_type": "playlist",
            "entries": [
                {"title": "ok", "webpage_url": "https://page.example.test/w/1"},
                {"title": "no url"},
                {"title": "ok2", "url": "https://page.example.test/w/2"},
            ],
        }
        with patch.object(resolver, "_extract", return_value=mixed):
            probe = resolver.probe_batch("https://page.example.test/list")

        self.assertEqual(len(probe.items), 2)

    def test_the_probe_uses_flat_extraction(self) -> None:
        # Flat extraction skips per-entry format resolution, which is what makes
        # the probe cheap enough to run on every paste.
        resolver = yr.YtDlpResolver()
        with patch.object(resolver, "_extract", return_value=PLAYLIST) as extract:
            resolver.probe_batch("https://page.example.test/list")

        self.assertTrue(extract.call_args.kwargs["flat"])

    def test_hitting_the_limit_marks_the_result_truncated(self) -> None:
        resolver = yr.YtDlpResolver(yr.YtDlpOptions(playlist_probe_limit=2))
        with patch.object(resolver, "_extract", return_value=PLAYLIST):
            probe = resolver.probe_batch("https://page.example.test/list")

        self.assertTrue(probe.truncated)


class ParamBuildingTests(unittest.TestCase):
    def test_flat_probing_sets_the_entry_cap(self) -> None:
        params = yr._base_params(yr.YtDlpOptions(playlist_probe_limit=50), flat=True)

        self.assertEqual(params["extract_flat"], "in_playlist")
        self.assertEqual(params["playlistend"], 50)

    def test_a_full_resolve_does_not_flatten(self) -> None:
        params = yr._base_params(yr.YtDlpOptions(), flat=False)

        self.assertNotIn("extract_flat", params)

    def test_a_socket_timeout_is_always_set(self) -> None:
        # An extractor that hangs must become a decline, not a stuck job.
        params = yr._base_params(yr.YtDlpOptions(socket_timeout=7))

        self.assertEqual(params["socket_timeout"], 7)

    def test_browser_cookies_are_off_unless_configured(self) -> None:
        self.assertNotIn("cookiesfrombrowser", yr._base_params(yr.YtDlpOptions()))

        params = yr._base_params(yr.YtDlpOptions(cookies_from_browser="chrome"))

        self.assertEqual(params["cookiesfrombrowser"], ("chrome",))


if __name__ == "__main__":
    unittest.main()
