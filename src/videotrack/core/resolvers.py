from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qsl, unquote, urlparse

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


#: Path segments that mean "sign in before going further". Matched as whole
#: segments, never as substrings, so a page at /login-guide and an /authors
#: listing are not mistaken for an access wall.
AUTH_PATH_SEGMENTS = frozenset(
    {
        "login",
        "signin",
        "sign-in",
        "sign_in",
        "signup",
        "sign-up",
        "register",
        "auth",
        "authenticate",
        "oauth",
    }
)

#: Query parameters a sign-in page commonly uses to remember where to send the
#: visitor back. Corroboration for the message only, never a deciding signal:
#: plenty of sign-in pages do not add one.
RETURN_PARAM_NAMES = frozenset(
    {"currenturl", "next", "return", "returnurl", "returnto", "redirect", "redirecturi", "continue"}
)


def _auth_segments(url: str) -> list[str]:
    segments = [part for part in (urlparse(url).path or "").lower().split("/") if part]
    return [part for part in segments if part in AUTH_PATH_SEGMENTS]


def _carries_return_to(query: str, requested_path: str) -> bool:
    if not requested_path or requested_path == "/":
        return False
    for name, value in parse_qsl(query, keep_blank_values=True):
        flattened = "".join(char for char in name.lower() if char.isalnum())
        if flattened in RETURN_PARAM_NAMES and requested_path in unquote(value):
            return True
    return False


def auth_wall_reason(requested_url: str, resolution: Resolution) -> str | None:
    """Describe why this resolution is a sign-in wall, or None if it is not.

    Three conditions have to hold together, and each one carries weight:

    1. The page went somewhere else. A page that stayed put is whatever it
       always was.
    2. The destination has an auth path segment, matched whole.
    3. Nothing directly fetchable was found.

    The third is the one that protects the common case. A resolution with no
    media is **normal** for a page that serves its stream from an embed - the
    browser engine returns the capture precisely so the pipeline can deep-scan
    it - so an empty media tuple must never trigger a refusal on its own, and a
    redirect that still yields media is not a wall either.

    Nothing here tries to get past the wall. The point is to say so plainly
    instead of reporting success and queueing work against a sign-in page.
    """
    if resolution.media:
        return None

    requested = urlparse(requested_url)
    final = urlparse(resolution.final_url or "")
    if not final.netloc or (final.netloc, final.path) == (requested.netloc, requested.path):
        return None

    if not _auth_segments(resolution.final_url):
        return None

    reason = f"the page redirected to a sign-in page at {resolution.final_url}"
    if _carries_return_to(final.query, requested.path):
        reason += ", which asks to return here afterwards"
    return reason
