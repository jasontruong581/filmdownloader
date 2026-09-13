"""Reading an HLS playlist far enough to fetch it by hand.

The repack path exists for playlists FFmpeg will not read, and it used to treat
every line that did not start with `#` as a media segment. That is true only of
the simplest playlist there is. Everything else a real stream carries lives on
exactly those `#` lines:

  * `#EXT-X-KEY` names the AES-128 key the segments are encrypted with. Ignored,
    the repack writes ciphertext into a container and calls it a download.
  * `#EXT-X-MAP` names the initialisation segment an fMP4 stream needs before
    any media segment means anything. Ignored, the output will not play.
  * `#EXT-X-STREAM-INF` marks a *master* playlist, whose non-comment lines are
    other playlists rather than media. Ignored, the repack concatenates a
    handful of text files and hands them to FFmpeg.
  * `#EXT-X-BYTERANGE` makes a segment a slice of a larger resource. Ignored,
    the same resource is fetched once per slice and the output is that file
    repeated.

None of this is site-specific, so it belongs in `core`: it is what the format
says, and the payload-level tricks stay where they were.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin

#: Attribute lists are comma-separated, and a quoted value may contain a comma.
_ATTRIBUTE = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')

#: The only method this path can hand to FFmpeg as a local playlist.
METHOD_NONE = "NONE"
METHOD_AES_128 = "AES-128"


@dataclass(frozen=True)
class Key:
    """An `#EXT-X-KEY`, in force until the next one."""

    method: str
    uri: str | None = None
    iv: str | None = None

    @property
    def encrypted(self) -> bool:
        return self.method != METHOD_NONE and self.uri is not None


@dataclass(frozen=True)
class Segment:
    """One media segment, carrying whatever was in force where it appeared."""

    uri: str
    duration: float
    key: Key | None = None
    init_uri: str | None = None
    #: (length, offset) when the segment is a slice of a larger resource.
    byterange: tuple[int, int] | None = None


@dataclass(frozen=True)
class Variant:
    """One `#EXT-X-STREAM-INF` entry of a master playlist."""

    uri: str
    bandwidth: int
    resolution: str | None = None


@dataclass(frozen=True)
class Playlist:
    segments: tuple[Segment, ...] = ()
    variants: tuple[Variant, ...] = ()
    #: Preserved because an `#EXT-X-KEY` without an explicit IV uses the
    #: segment's sequence number as one. Renumber the segments and every
    #: implicit IV changes, which decrypts to noise rather than to an error.
    media_sequence: int = 0
    target_duration: int = 10

    @property
    def is_master(self) -> bool:
        return bool(self.variants) and not self.segments


def _attributes(raw: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for name, value in _ATTRIBUTE.findall(raw):
        found[name] = value[1:-1] if value.startswith('"') else value
    return found


def _byterange(raw: str, previous_end: int | None) -> tuple[int, int]:
    """`<length>[@<offset>]`, where a missing offset continues the last slice."""
    length, _, offset = raw.partition("@")
    start = int(offset) if offset else (previous_end or 0)
    return int(length), start


def _int(raw: str, default: int) -> int:
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def parse_playlist(text: str, base_url: str) -> Playlist:
    """Read a playlist, resolving every URI it names against `base_url`.

    Both kinds come back as one type. A master playlist has variants and no
    segments; asking for `is_master` is how a caller tells them apart, rather
    than guessing from whether the first URI happens to end in `.m3u8`.
    """
    segments: list[Segment] = []
    variants: list[Variant] = []
    media_sequence = 0
    target_duration = 10

    key: Key | None = None
    init_uri: str | None = None
    duration = 0.0
    byterange: tuple[int, int] | None = None
    previous_end: int | None = None
    pending_variant: Variant | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if not line.startswith("#"):
            url = urljoin(base_url, line)
            if pending_variant is not None:
                variants.append(
                    Variant(
                        uri=url,
                        bandwidth=pending_variant.bandwidth,
                        resolution=pending_variant.resolution,
                    )
                )
                pending_variant = None
                continue
            segments.append(
                Segment(
                    uri=url,
                    duration=duration,
                    key=key,
                    init_uri=init_uri,
                    byterange=byterange,
                )
            )
            if byterange is not None:
                previous_end = byterange[1] + byterange[0]
            duration = 0.0
            byterange = None
            continue

        tag, _, value = line.partition(":")
        if tag == "#EXTINF":
            duration = float(value.split(",")[0] or 0)
        elif tag == "#EXT-X-KEY":
            attrs = _attributes(value)
            method = attrs.get("METHOD", METHOD_NONE)
            uri = attrs.get("URI")
            key = Key(
                method=method,
                uri=urljoin(base_url, uri) if uri else None,
                iv=attrs.get("IV"),
            )
        elif tag == "#EXT-X-MAP":
            attrs = _attributes(value)
            mapped = attrs.get("URI")
            init_uri = urljoin(base_url, mapped) if mapped else None
        elif tag == "#EXT-X-BYTERANGE":
            byterange = _byterange(value, previous_end)
        elif tag == "#EXT-X-MEDIA-SEQUENCE":
            media_sequence = _int(value, 0)
        elif tag == "#EXT-X-TARGETDURATION":
            target_duration = _int(value, 10)
        elif tag == "#EXT-X-STREAM-INF":
            attrs = _attributes(value)
            pending_variant = Variant(
                uri="",
                bandwidth=_int(attrs.get("BANDWIDTH", ""), 0),
                resolution=attrs.get("RESOLUTION"),
            )

    return Playlist(
        segments=tuple(segments),
        variants=tuple(variants),
        media_sequence=media_sequence,
        target_duration=target_duration,
    )


def best_variant(playlist: Playlist) -> Variant | None:
    """The highest-bandwidth rendition, which is the one worth downloading."""
    if not playlist.variants:
        return None
    return max(playlist.variants, key=lambda variant: variant.bandwidth)


def unsupported_methods(playlist: Playlist) -> tuple[str, ...]:
    """Encryption methods this path cannot hand to FFmpeg, in playlist order.

    SAMPLE-AES encrypts inside the elementary stream rather than the segment,
    so writing the segments out as files and pointing FFmpeg at them does not
    reach it. Reported rather than attempted, because attempting it produces a
    file that exists, has the right size, and is noise.
    """
    seen: list[str] = []
    for segment in playlist.segments:
        method = segment.key.method if segment.key else METHOD_NONE
        if method in (METHOD_NONE, METHOD_AES_128) or method in seen:
            continue
        seen.append(method)
    return tuple(seen)
