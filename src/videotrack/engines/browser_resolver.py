"""Chrome network capture as the compatibility engine.

This is the fallback that works without knowing anything about a page: drive a
real browser, record what it fetched, and let the shared detection pipeline pick
the media out. It is the slowest engine and the only one that needs Selenium, so
it runs last.

The produced Resolution carries the capture itself rather than a synthesized one,
so the pipeline keeps the full request list, the embed URLs it deep-scans, and
the cross-domain cookies the capture collected.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.capture import capture_page
from ..core.resolvers import Resolution, ResolvedMedia, media_kind

ENGINE_NAME = "browser"


@dataclass(frozen=True)
class BrowserOptions:
    wait_seconds: int = 15
    headless: bool = True
    try_play: bool = True


class BrowserResolver:
    """Wraps browser capture in the resolver protocol."""

    name = ENGINE_NAME

    def __init__(self, options: BrowserOptions | None = None) -> None:
        self.options = options or BrowserOptions()

    def resolve(self, url: str) -> Resolution | None:
        capture = capture_page(
            url=url,
            wait_seconds=self.options.wait_seconds,
            headless=self.options.headless,
            try_play=self.options.try_play,
        )
        media = tuple(
            ResolvedMedia(
                request.url,
                request.headers.get("Referer") or capture.final_url,
                media_kind(request.url),
            )
            for request in capture.requests
            if request.resource_type == "Media"
        )
        return Resolution(
            resolver=ENGINE_NAME,
            page_url=url,
            final_url=capture.final_url,
            title=capture.title,
            media=media,
            user_agent=capture.user_agent,
            cookies=capture.cookies,
            engine=ENGINE_NAME,
            capture=capture,
        )

    def resolve_many(self, url: str) -> list[Resolution]:
        resolution = self.resolve(url)
        return [resolution] if resolution is not None else []
