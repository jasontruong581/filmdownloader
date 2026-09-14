"""Reading a playlist past the lines that do not start with `#`.

The repack used to keep every line that was not a comment and call the result
segments. That is only true of the simplest playlist there is, and each thing
it dropped fails differently: a master playlist turns into a handful of text
files, an encrypted one into ciphertext in a container, an fMP4 one into media
with no header, and a byte-ranged one into the same file over and over.

No network here: these are strings.
"""

from __future__ import annotations

import unittest

from videotrack.core import hls

BASE = "https://cdn.example.test/hls/index.m3u8"

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360,CODECS="avc1.4d401e,mp4a.40.2"
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2400000,RESOLUTION=1280x720,CODECS="avc1.4d401f,mp4a.40.2"
720/index.m3u8
"""

ENCRYPTED = """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:10
#EXT-X-MEDIA-SEQUENCE:7
#EXT-X-KEY:METHOD=AES-128,URI="key.bin",IV=0x0000000000000000000000000000000A
#EXTINF:9.009,
seg0.ts
#EXTINF:9.009,
seg1.ts
#EXT-X-ENDLIST
"""

FRAGMENTED = """#EXTM3U
#EXT-X-TARGETDURATION:4
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.000,
seg0.m4s
#EXTINF:4.000,
seg1.m4s
#EXT-X-ENDLIST
"""

BYTERANGE = """#EXTM3U
#EXTINF:9.009,
#EXT-X-BYTERANGE:75232@0
all.ts
#EXTINF:9.009,
#EXT-X-BYTERANGE:82112
all.ts
#EXT-X-ENDLIST
"""


class MasterPlaylistTests(unittest.TestCase):
    def test_a_master_playlist_is_not_mistaken_for_segments(self) -> None:
        playlist = hls.parse_playlist(MASTER, BASE)

        self.assertTrue(playlist.is_master)
        self.assertEqual(playlist.segments, ())

    def test_variant_uris_resolve_against_the_playlist(self) -> None:
        playlist = hls.parse_playlist(MASTER, BASE)

        self.assertEqual(
            [variant.uri for variant in playlist.variants],
            [
                "https://cdn.example.test/hls/360/index.m3u8",
                "https://cdn.example.test/hls/720/index.m3u8",
            ],
        )

    def test_a_comma_inside_a_quoted_attribute_does_not_split_it(self) -> None:
        # CODECS carries a comma, which a naive split on "," reads as the end of
        # the attribute list - and BANDWIDTH is what picks the rendition.
        playlist = hls.parse_playlist(MASTER, BASE)

        self.assertEqual([variant.bandwidth for variant in playlist.variants], [800000, 2400000])

    def test_the_best_variant_is_the_highest_bandwidth(self) -> None:
        best = hls.best_variant(hls.parse_playlist(MASTER, BASE))

        self.assertIsNotNone(best)
        assert best is not None
        self.assertEqual(best.uri, "https://cdn.example.test/hls/720/index.m3u8")

    def test_a_media_playlist_has_no_best_variant(self) -> None:
        self.assertIsNone(hls.best_variant(hls.parse_playlist(ENCRYPTED, BASE)))


class EncryptionTests(unittest.TestCase):
    def test_the_key_reaches_every_segment_it_covers(self) -> None:
        playlist = hls.parse_playlist(ENCRYPTED, BASE)

        for segment in playlist.segments:
            self.assertIsNotNone(segment.key)
            assert segment.key is not None
            self.assertEqual(segment.key.method, "AES-128")
            self.assertEqual(segment.key.uri, "https://cdn.example.test/hls/key.bin")

    def test_an_explicit_iv_is_kept_verbatim(self) -> None:
        # Rewriting it would change what the segments decrypt to.
        segment = hls.parse_playlist(ENCRYPTED, BASE).segments[0]

        assert segment.key is not None
        self.assertEqual(segment.key.iv, "0x0000000000000000000000000000000A")

    def test_the_media_sequence_is_kept(self) -> None:
        # A key with no explicit IV takes one from the sequence number, so
        # renumbering the segments decrypts them to noise rather than to error.
        self.assertEqual(hls.parse_playlist(ENCRYPTED, BASE).media_sequence, 7)

    def test_a_key_can_be_turned_off_partway(self) -> None:
        text = ENCRYPTED.replace(
            "#EXTINF:9.009,\nseg1.ts",
            "#EXT-X-KEY:METHOD=NONE\n#EXTINF:9.009,\nseg1.ts",
        )
        segments = hls.parse_playlist(text, BASE).segments

        assert segments[0].key is not None and segments[1].key is not None
        self.assertTrue(segments[0].key.encrypted)
        self.assertFalse(segments[1].key.encrypted)

    def test_sample_aes_is_named_as_unsupported(self) -> None:
        # It encrypts inside the elementary stream, so writing segments out as
        # files and pointing FFmpeg at them never reaches it. Attempting it
        # produces a file of the right size that is noise.
        text = ENCRYPTED.replace("METHOD=AES-128", "METHOD=SAMPLE-AES")

        self.assertEqual(hls.unsupported_methods(hls.parse_playlist(text, BASE)), ("SAMPLE-AES",))

    def test_aes_128_and_plaintext_are_both_supported(self) -> None:
        self.assertEqual(hls.unsupported_methods(hls.parse_playlist(ENCRYPTED, BASE)), ())
        self.assertEqual(hls.unsupported_methods(hls.parse_playlist(FRAGMENTED, BASE)), ())


class FragmentedTests(unittest.TestCase):
    def test_the_init_segment_reaches_every_segment(self) -> None:
        playlist = hls.parse_playlist(FRAGMENTED, BASE)

        for segment in playlist.segments:
            self.assertEqual(segment.init_uri, "https://cdn.example.test/hls/init.mp4")

    def test_a_playlist_without_one_reports_none(self) -> None:
        self.assertIsNone(hls.parse_playlist(ENCRYPTED, BASE).segments[0].init_uri)


class ByterangeTests(unittest.TestCase):
    def test_an_explicit_offset_is_read(self) -> None:
        self.assertEqual(hls.parse_playlist(BYTERANGE, BASE).segments[0].byterange, (75232, 0))

    def test_a_missing_offset_continues_the_previous_slice(self) -> None:
        # Which is what makes two segments of the same URI different bytes.
        self.assertEqual(hls.parse_playlist(BYTERANGE, BASE).segments[1].byterange, (82112, 75232))

    def test_both_slices_name_the_same_resource(self) -> None:
        segments = hls.parse_playlist(BYTERANGE, BASE).segments

        self.assertEqual(segments[0].uri, segments[1].uri)


class ShapeTests(unittest.TestCase):
    def test_durations_are_read(self) -> None:
        self.assertEqual(
            [segment.duration for segment in hls.parse_playlist(ENCRYPTED, BASE).segments],
            [9.009, 9.009],
        )

    def test_target_duration_defaults_when_absent(self) -> None:
        self.assertEqual(hls.parse_playlist("#EXTM3U\nseg.ts\n", BASE).target_duration, 10)

    def test_an_empty_playlist_yields_nothing_rather_than_raising(self) -> None:
        playlist = hls.parse_playlist("", BASE)

        self.assertEqual(playlist.segments, ())
        self.assertFalse(playlist.is_master)

    def test_relative_segment_uris_resolve_against_the_playlist(self) -> None:
        segments = hls.parse_playlist(ENCRYPTED, BASE).segments

        self.assertEqual(segments[0].uri, "https://cdn.example.test/hls/seg0.ts")


if __name__ == "__main__":
    unittest.main()
