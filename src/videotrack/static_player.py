from __future__ import annotations

import html
import json
import re
from urllib.parse import urlencode, urljoin, urlparse

import requests

from .resolvers import DEFAULT_USER_AGENT, Resolution, ResolvedMedia, media_kind

MEDIA_URL_RE = re.compile(r"https?://[^\"'\s<>]+(?:m3u8|mp4|mpd)[^\"'\s<>]*", re.IGNORECASE)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value))).strip()


def _page_title(page_html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
    return _clean_text(match.group(1)) if match else ""


def extract_media_urls(player_html: str, base_url: str) -> list[str]:
    """Extract direct and JW-style media declarations from static player markup."""
    found: dict[str, None] = {}
    for match in MEDIA_URL_RE.finditer(player_html):
        found[html.unescape(match.group(0))] = None

    for value in re.findall(r"(?:file|src)\s*:\s*[\"']([^\"']+)[\"']", player_html, re.IGNORECASE):
        url = urljoin(base_url, html.unescape(value))
        if re.search(r"(?:m3u8|mp4|mpd)(?:$|[?&#])", url, re.IGNORECASE):
            found[url] = None
    return list(found)


def _movie_config(page_html: str) -> tuple[str | None, str | None]:
    for pattern in (
        r'data-movie=["\']([^"\']+)["\'][^>]*data-type=["\']([^"\']+)["\']',
        r'data-type=["\']([^"\']+)["\'][^>]*data-movie=["\']([^"\']+)["\']',
    ):
        match = re.search(pattern, page_html, re.IGNORECASE)
        if not match:
            continue
        return (match.group(1), match.group(2)) if "data-movie" in pattern[:20] else (match.group(2), match.group(1))
    return None, None


class StaticPlayerResolver:
    """Resolves pages that expose a player endpoint in their HTML markup."""

    name = "static-player"

    def __init__(self, timeout: int = 20) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": DEFAULT_USER_AGENT})

    def _resolve_embed(self, embed_url: str) -> list[ResolvedMedia]:
        try:
            response = self.session.get(embed_url, headers={"Referer": embed_url}, timeout=self.timeout)
            response.raise_for_status()
        except requests.RequestException:
            return []

        urls = extract_media_urls(response.text or "", embed_url)
        if urls:
            return [ResolvedMedia(url, embed_url, media_kind(url)) for url in urls]

        id_match = re.search(r"window\.videoId\s*=\s*['\"]([^'\"]+)['\"]", response.text or "", re.IGNORECASE)
        if not id_match:
            return []
        parsed = urlparse(embed_url)
        api_root = f"{parsed.scheme}://{parsed.netloc}/api/get-video?id={id_match.group(1)}"
        for counter in range(6):
            try:
                api = self.session.get(f"{api_root}&counter={counter}", headers={"Referer": embed_url}, timeout=self.timeout)
                api.raise_for_status()
                data = api.json()
            except (requests.RequestException, json.JSONDecodeError):
                continue
            for key in ("url", "file", "src"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    return [ResolvedMedia(value, embed_url, media_kind(value))]
        return []

    def resolve(self, url: str) -> Resolution | None:
        try:
            page = self.session.get(url, timeout=self.timeout)
            page.raise_for_status()
        except requests.RequestException:
            return None

        page_html = page.text or ""
        movie_id, movie_type = _movie_config(page_html)
        if not movie_id or not movie_type:
            return None

        root = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}/"
        endpoint = urljoin(root, "get.xvideo.php" if movie_type == "10" else "get.video.php")
        try:
            player = self.session.post(
                endpoint,
                data=urlencode({"movie_id": movie_id, "type": movie_type, "index": "1"}),
                headers={
                    "Referer": page.url,
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                timeout=self.timeout,
            )
            player.raise_for_status()
        except requests.RequestException:
            return None

        media = [ResolvedMedia(item, endpoint, media_kind(item)) for item in extract_media_urls(player.text or "", endpoint)]
        if not media:
            iframe = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', player.text or "", re.IGNORECASE)
            if iframe:
                media = self._resolve_embed(urljoin(endpoint, html.unescape(iframe.group(1))))
        if not media:
            return None
        cookies = {cookie.name: cookie.value for cookie in self.session.cookies}
        return Resolution(self.name, url, page.url, _page_title(page_html), tuple(media), cookies=cookies)
