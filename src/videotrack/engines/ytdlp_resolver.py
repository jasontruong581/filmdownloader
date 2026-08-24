"""yt-dlp as the primary resolution engine.

yt-dlp is used as a library rather than a subprocess so errors and progress
arrive structured. Every field is read with `.get()` through one mapping
function: yt-dlp's info dict changes shape between releases, and a renamed field
should degrade a label rather than raise.

Declining is not an error here. An unsupported page, a network failure, or an
empty format list all return an empty list so the chain moves on to the next
engine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..core.models import BatchItem, BatchProbe, MediaFormat, sort_formats
from ..core.resolvers import DEFAULT_USER_AGENT, Resolution, ResolvedMedia, media_kind

ENGINE_NAME = "ytdlp"

#: Cap on how many playlist entries a batch probe enumerates in one call.
DEFAULT_PLAYLIST_PROBE_LIMIT = 200

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class YtDlpOptions:
    socket_timeout: int = 20
    playlist_probe_limit: int = DEFAULT_PLAYLIST_PROBE_LIMIT
    cookies_from_browser: str | None = None
    user_agent: str = DEFAULT_USER_AGENT


def _ytdlp_module():
    """Import yt-dlp lazily so the package works without it installed."""
    try:
        import yt_dlp
    except ImportError:  # pragma: no cover - depends on the environment
        return None
    return yt_dlp


def _base_params(options: YtDlpOptions, *, flat: bool = False) -> dict:
    params: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "socket_timeout": options.socket_timeout,
        # Playlists are expanded deliberately by the caller, never silently.
        "noplaylist": False,
        "logger": logging.getLogger(f"{__name__}.ytdlp"),
    }
    if flat:
        params["extract_flat"] = "in_playlist"
        params["playlistend"] = options.playlist_probe_limit
    if options.cookies_from_browser:
        # Best effort: reuses a browser session the operator already holds. On
        # current Chrome, app-bound cookie encryption can make this fail.
        params["cookiesfrombrowser"] = (options.cookies_from_browser,)
    return params


def _as_int(value) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _map_format(raw: dict) -> MediaFormat | None:
    """Translate one yt-dlp format dict. Missing fields degrade, never raise."""
    format_id = raw.get("format_id")
    if not format_id:
        return None
    if raw.get("url") in (None, ""):
        return None
    return MediaFormat(
        format_id=str(format_id),
        container=raw.get("ext") or None,
        height=_as_int(raw.get("height")),
        width=_as_int(raw.get("width")),
        fps=_as_float(raw.get("fps")),
        vcodec=raw.get("vcodec") or None,
        acodec=raw.get("acodec") or None,
        filesize=_as_int(raw.get("filesize")),
        filesize_approx=_as_int(raw.get("filesize_approx")),
        tbr=_as_float(raw.get("tbr")),
        note=raw.get("format_note") or None,
    )


def map_formats(info: dict) -> tuple[MediaFormat, ...]:
    formats = []
    for raw in info.get("formats") or []:
        if not isinstance(raw, dict):
            continue
        mapped = _map_format(raw)
        if mapped is not None:
            formats.append(mapped)
    return sort_formats(formats)


def _media_from_info(info: dict, page_url: str) -> tuple[ResolvedMedia, ...]:
    """Direct URLs for the FFmpeg path, best rendition first."""
    referer = info.get("webpage_url") or page_url
    urls: list[ResolvedMedia] = []
    seen: set[str] = set()

    for key in ("url", "manifest_url"):
        value = info.get(key)
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            urls.append(ResolvedMedia(value, referer, media_kind(value)))

    for raw in info.get("formats") or []:
        if not isinstance(raw, dict):
            continue
        value = raw.get("url")
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            urls.append(ResolvedMedia(value, referer, media_kind(value)))
    return tuple(urls)


def resolution_from_info(info: dict, page_url: str, options: YtDlpOptions) -> Resolution | None:
    formats = map_formats(info)
    media = _media_from_info(info, page_url)
    if not formats and not media:
        return None

    return Resolution(
        resolver=ENGINE_NAME,
        page_url=page_url,
        final_url=info.get("webpage_url") or page_url,
        title=(info.get("title") or "").strip(),
        media=media,
        user_agent=options.user_agent,
        cookies=None,
        engine=ENGINE_NAME,
        formats=formats,
        duration=_as_float(info.get("duration")),
        thumbnail=info.get("thumbnail") or None,
        uploader=info.get("uploader") or info.get("channel") or None,
    )


def _playlist_entries(info: dict) -> list[dict]:
    return [entry for entry in (info.get("entries") or []) if isinstance(entry, dict)]


def _entry_url(entry: dict) -> str | None:
    for key in ("webpage_url", "url", "original_url"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
    return None


class YtDlpResolver:
    """Resolves any URL yt-dlp's extractors recognize."""

    name = ENGINE_NAME

    def __init__(self, options: YtDlpOptions | None = None) -> None:
        self.options = options or YtDlpOptions()

    @property
    def available(self) -> bool:
        return _ytdlp_module() is not None

    def _extract(self, url: str, *, flat: bool) -> dict | None:
        module = _ytdlp_module()
        if module is None:
            return None
        try:
            with module.YoutubeDL(_base_params(self.options, flat=flat)) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001
            # Unsupported URL, network failure, timeout, geo block: all are a
            # decline, so the chain can try the next engine.
            logger.debug("yt-dlp declined %s: %s", url, exc)
            return None
        return info if isinstance(info, dict) else None

    def resolve_many(self, url: str) -> list[Resolution]:
        """Every downloadable item at this URL. A single video yields one."""
        info = self._extract(url, flat=False)
        if info is None:
            return []

        if info.get("_type") == "playlist":
            resolutions = []
            for entry in _playlist_entries(info):
                entry_url = _entry_url(entry) or url
                resolution = resolution_from_info(entry, entry_url, self.options)
                if resolution is not None:
                    resolutions.append(resolution)
            return resolutions

        resolution = resolution_from_info(info, url, self.options)
        return [resolution] if resolution is not None else []

    def resolve(self, url: str) -> Resolution | None:
        """Single-item resolution, for the Resolver protocol."""
        resolutions = self.resolve_many(url)
        return resolutions[0] if resolutions else None

    def probe_batch(self, url: str) -> BatchProbe:
        """Cheaply ask whether this URL enumerates several items.

        Flat extraction skips per-entry format resolution, so this costs a
        fraction of a real resolve.
        """
        info = self._extract(url, flat=True)
        if info is None:
            return BatchProbe(reason="yt-dlp does not recognize this URL")

        if info.get("_type") != "playlist":
            return BatchProbe(reason="yt-dlp resolved a single video, not a playlist")

        entries = _playlist_entries(info)
        items = []
        for entry in entries:
            entry_url = _entry_url(entry)
            if not entry_url:
                continue
            items.append(BatchItem(url=entry_url, title=(entry.get("title") or "").strip()))

        if len(items) < 2:
            return BatchProbe(reason="this playlist lists fewer than two usable entries")

        total = _as_int(info.get("playlist_count")) or len(items)
        return BatchProbe(
            capability="playlist",
            confidence="proven",
            items=tuple(items),
            total_estimate=total,
            truncated=len(items) >= self.options.playlist_probe_limit,
            reason="",
        )
