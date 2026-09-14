"""Reusable pieces for a host that serves a player page.

Most streaming sites do not host their own video. They embed one of a small
number of player hosts, which makes the *host* rather than the site the unit
worth supporting: one plugin for an embed host reaches every site that embeds
it, while one plugin per site reaches one site.

What such a plugin needs is the same every time - fetch the player page, read
the media declarations out of its markup, and follow a nested frame when the
first page only points at another one. All of that already existed, inside the
`vlxx` plugin, where no second host could reach it.

This module registers nothing. It is what a plugin is built from, and the
plugin modules `sites/__init__` imports are what get registered.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin, urlparse

import requests

from ..core.resolvers import DEFAULT_USER_AGENT, Resolution, ResolvedMedia, media_kind
from . import BaseSitePlugin

#: A media URL written out in full anywhere in the markup.
MEDIA_URL_RE = re.compile(r"https?://[^\"'\s<>]+(?:m3u8|mp4|mpd)[^\"'\s<>]*", re.IGNORECASE)

#: The JW-style declaration, which is usually relative to the player page.
MEDIA_DECLARATION_RE = re.compile(r"(?:file|src)\s*:\s*[\"']([^\"']+)[\"']", re.IGNORECASE)

#: A media extension at the end of a URL, or before its query or fragment.
MEDIA_EXTENSION_RE = re.compile(r"(?:m3u8|mp4|mpd)(?:$|[?&#])", re.IGNORECASE)

IFRAME_SRC_RE = re.compile(r"<iframe[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

#: How many nested frames to follow. Player hosts wrap their player in one
#: frame often and in two occasionally; a page that keeps pointing at another
#: page is a redirect loop rather than a player, and following it is a request
#: storm rather than a download.
MAX_FRAME_HOPS = 2


def clean_text(value: str) -> str:
    """Markup and entities out, runs of whitespace collapsed."""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value))).strip()


def page_title(page_html: str) -> str:
    match = TITLE_RE.search(page_html)
    return clean_text(match.group(1)) if match else ""


def extract_media_urls(player_html: str, base_url: str) -> list[str]:
    """Extract direct and JW-style media declarations from static player markup."""
    found: dict[str, None] = {}
    for match in MEDIA_URL_RE.finditer(player_html):
        found[html.unescape(match.group(0))] = None

    for value in MEDIA_DECLARATION_RE.findall(player_html):
        url = urljoin(base_url, html.unescape(value))
        if MEDIA_EXTENSION_RE.search(url):
            found[url] = None
    return list(found)


def extract_iframe_urls(player_html: str, base_url: str) -> list[str]:
    """Frames the markup declares, resolved against the page that declared them."""
    found: dict[str, None] = {}
    for value in IFRAME_SRC_RE.findall(player_html):
        found[urljoin(base_url, html.unescape(value))] = None
    return list(found)


def media_from(player_html: str, base_url: str) -> list[ResolvedMedia]:
    """Whatever the markup declares, as media this package can download."""
    return [
        ResolvedMedia(url, base_url, media_kind(url))
        for url in extract_media_urls(player_html, base_url)
    ]


def fetch_page(
    url: str,
    session: requests.Session,
    timeout: int = 20,
    referer: str | None = None,
) -> str | None:
    """A page's markup, or None when it could not be fetched.

    A player host that refuses the request is a page this resolver does not
    recognise, which is the same answer as markup it cannot read. Neither is an
    error worth raising past the resolver: the chain moves to the next engine.
    """
    try:
        response = session.get(url, headers={"Referer": referer or url}, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException:
        return None
    return response.text or ""


class EmbedResolver:
    """Reads the media a player page declares, following a nested frame or two.

    Static: one HTTP request per page rather than a browser session. That is
    the whole point of recognising the host - the deep scan's fallback is to
    start Chrome and wait, which costs thirty to sixty seconds per embed.
    """

    def __init__(self, name: str = "embed", timeout: int = 20) -> None:
        self.name = name
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    def _media_with_frames(self, url: str, page_html: str) -> tuple[list[ResolvedMedia], str]:
        """Media from this page, else from the frames it points at."""
        media = media_from(page_html, url)
        if media:
            return media, url

        seen = {url}
        frontier = [(frame, url) for frame in extract_iframe_urls(page_html, url)]
        for _ in range(MAX_FRAME_HOPS):
            next_frontier: list[tuple[str, str]] = []
            for frame_url, referer in frontier:
                if frame_url in seen:
                    continue
                seen.add(frame_url)
                nested = fetch_page(frame_url, self.session, self.timeout, referer=referer)
                if nested is None:
                    continue
                media = media_from(nested, frame_url)
                if media:
                    return media, frame_url
                next_frontier.extend(
                    (deeper, frame_url) for deeper in extract_iframe_urls(nested, frame_url)
                )
            frontier = next_frontier
            if not frontier:
                break
        return [], url

    def resolve(self, url: str) -> Resolution | None:
        page_html = fetch_page(url, self.session, self.timeout)
        if page_html is None:
            return None

        media, _ = self._media_with_frames(url, page_html)
        if not media:
            return None

        cookies = {cookie.name: cookie.value for cookie in self.session.cookies}
        return Resolution(
            self.name,
            url,
            url,
            page_title(page_html),
            tuple(media),
            cookies=cookies,
        )


class EmbedHostPlugin(BaseSitePlugin):
    """A plugin for one player host.

    A subclass declares `name` and the hostnames it serves, and calls
    `register()` on an instance of itself. Everything else - the URL prefilter,
    the static resolver, the frame following - is the same for every host,
    which is what makes the next one cheap rather than another site plugin.

    `handles` stays a URL-only prefilter, as the registry requires: whether the
    markup is recognised is decided by the resolver, which already has the page.
    """

    name = "embed-host"

    #: Registrable domains this host serves players from. A subdomain of one
    #: counts, because these hosts rotate them freely.
    hosts: tuple[str, ...] = ()

    def handles(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return False
        return any(host == known or host.endswith(f".{known}") for known in self.hosts)

    def resolver(self) -> EmbedResolver:
        return EmbedResolver(name=self.name)
