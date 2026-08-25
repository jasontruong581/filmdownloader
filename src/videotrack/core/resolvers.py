from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from .models import CaptureResult, MediaFormat, NetworkRequest

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; FilmDownloader/1.0)"


@dataclass(frozen=True)
class ResolvedMedia:
    url: str
    referer: str
    kind: str


@dataclass(frozen=True)
class Resolution:
    """What an engine learned about one downloadable item.

    `media` carries directly fetchable URLs, which is all the FFmpeg path needs.
    `formats` carries selectable renditions when the engine knows them; an engine
    that only finds one URL leaves it empty and the pipeline behaves as before.
    """

    resolver: str
    page_url: str
    final_url: str
    title: str
    media: tuple[ResolvedMedia, ...]
    user_agent: str = DEFAULT_USER_AGENT
    cookies: dict[str, str] | None = None
    engine: str = ""
    formats: tuple[MediaFormat, ...] = ()
    duration: float | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    #: Set when the engine produced a real browser capture. The pipeline reuses
    #: it verbatim so embed deep-scan and cross-domain cookies survive, instead
    #: of a synthesized capture holding only the media URLs.
    capture: CaptureResult | None = None

    def __post_init__(self) -> None:
        if not self.engine:
            object.__setattr__(self, "engine", self.resolver)


class Resolver(Protocol):
    name: str

    def resolve(self, url: str) -> Resolution | None:
        """Return a static resolution or None when the page is unsupported."""


def media_kind(url: str) -> str:
    path = (urlparse(url).path or "").lower()
    if ".m3u8" in path or "manifest" in path:
        return "hls"
    if ".mpd" in path:
        return "dash"
    return "mp4" if ".mp4" in path else "media"


def capture_from_resolution(resolution: Resolution) -> CaptureResult:
    """Shape a resolution into the capture the shared pipeline consumes."""
    if resolution.capture is not None:
        return resolution.capture

    requests = [
        NetworkRequest(
            url=item.url,
            method="GET",
            headers={"Referer": item.referer},
            resource_type="Media",
            status=200,
        )
        for item in resolution.media
    ]
    return CaptureResult(
        page_url=resolution.page_url,
        final_url=resolution.final_url,
        title=resolution.title,
        user_agent=resolution.user_agent,
        cookies=resolution.cookies or {},
        requests=requests,
    )
