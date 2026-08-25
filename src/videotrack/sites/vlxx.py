from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

import requests

from ..core.models import CrawlPreset, PageMetadata
from ..core.resolvers import DEFAULT_USER_AGENT, Resolution, ResolvedMedia, media_kind
from . import BaseSitePlugin, register

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


# --- Page metadata -----------------------------------------------------------
#
# These selectors describe one specific site family's markup. They used to run
# on every download in core, which named unrelated sites' files from fields that
# do not exist there.

def _clean_metadata_text(raw: str) -> str:
    """Strip tags, then unescape, then collapse whitespace.

    Order matters and matches the extractor this moved from: unescaping after
    tag removal leaves entity-encoded markup as text rather than re-parsing it.
    """
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return _clean_metadata_text(match.group(1)) if match else None


def extract_page_metadata(page_html: str) -> PageMetadata:
    """Pull code, title, cast, and description out of this family's markup."""
    code = _first_match(
        page_html,
        r'<span[^>]*class=["\'][^"\']*video-code[^"\']*["\'][^>]*>(.*?)</span>',
    )
    title = (
        _first_match(page_html, r'<h2[^>]*id=["\']page-title["\'][^>]*>(.*?)</h2>')
        or _first_match(page_html, r'<h2[^>]*class=["\'][^"\']*\bpage-title\b[^"\']*["\'][^>]*>(.*?)</h2>')
        or _first_match(page_html, r'<h2[^>]*class=["\'][^"\']*\bbreadcrumb\b[^"\']*["\'][^>]*>(.*?)</h2>')
    )
    description = _first_match(
        page_html,
        r'<div[^>]*class=["\'][^"\']*video-description[^"\']*["\'][^>]*>(.*?)</div>',
    )

    actresses: list[str] = []
    actresses_match = re.search(
        r'<div[^>]*class=["\'][^"\']*actress-tag[^"\']*["\'][^>]*>(.*?)</div>',
        page_html,
        re.IGNORECASE | re.DOTALL,
    )
    if actresses_match:
        raw_block = actresses_match.group(1)
        actresses = [
            _clean_metadata_text(name)
            for name in re.findall(r'title=["\']([^"\']+)["\']', raw_block, re.IGNORECASE)
            if _clean_metadata_text(name)
        ]
        if not actresses:
            plain = _clean_metadata_text(raw_block)
            actresses = [part.strip() for part in re.split(r"\s{2,}|,\s*", plain) if part.strip()]

    return PageMetadata(
        video_code=code or None,
        title=title or None,
        actresses=actresses or None,
        description=description or None,
    )


def write_description_sidecar(out_file: Path, metadata: PageMetadata) -> None:
    sidecar = out_file.with_name(f"{out_file.stem} description.txt")
    content = (
        f"title: {metadata.title or ''}\n"
        f"actress: {', '.join(metadata.actresses or [])}\n"
        f"Description: {metadata.description or ''}\n"
    )
    sidecar.write_text(content, encoding="utf-8")


class VlxxPlugin(BaseSitePlugin):
    """The static-player family that exposes data-movie/data-type markup.

    `handles` is a URL prefilter only. Whether a given page actually carries the
    expected markup is decided inside the resolver, which returns None when it
    does not, so the family stays broader than a single hostname.
    """

    name = "vlxx"

    def handles(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return host.startswith("vlxx.") or host == "vlxx" or ".vlxx." in f".{host}."

    def resolver(self):
        return StaticPlayerResolver()

    def metadata(self, capture, page_html: str | None = None) -> PageMetadata | None:
        if page_html is not None:
            return extract_page_metadata(page_html)

        url = capture.final_url or capture.page_url
        headers = {"User-Agent": capture.user_agent or DEFAULT_USER_AGENT, "Referer": url}
        if capture.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in capture.cookies.items())
        try:
            response = requests.get(url, headers=headers, timeout=20)
        except requests.RequestException:
            return PageMetadata()
        if not response.ok:
            return PageMetadata()
        if response.apparent_encoding:
            response.encoding = response.apparent_encoding
        return extract_page_metadata(response.text or "")

    def write_sidecar(self, out_file: Path, metadata: PageMetadata) -> None:
        write_description_sidecar(out_file, metadata)

    def crawl_preset(self) -> CrawlPreset:
        return CrawlPreset(name="vlxx", include_substring="/video/")


register(VlxxPlugin())
