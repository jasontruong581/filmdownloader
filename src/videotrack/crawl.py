from __future__ import annotations

import csv
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin, urldefrag, urlparse

import requests


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.hrefs.append(value)
                return


@dataclass(frozen=True)
class CrawlResult:
    matched_urls: list[str]
    visited_pages: int


@dataclass(frozen=True)
class CrawlPreset:
    name: str
    include_substring: str
    exclude_substrings: tuple[str, ...] = ()
    url_filter: Callable[[str, str], bool] | None = None


def _normalize_url(raw_url: str, base_url: str) -> str | None:
    resolved = urljoin(base_url, raw_url.strip())
    cleaned, _ = urldefrag(resolved)
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        return None
    return parsed.geturl()


def _extract_links(html: str, current_url: str) -> list[str]:
    parser = _AnchorParser()
    parser.feed(html)
    links: list[str] = []
    for href in parser.hrefs:
        normalized = _normalize_url(href, current_url)
        if normalized:
            links.append(normalized)
    return links


def _same_host(url: str, host: str) -> bool:
    return urlparse(url).netloc.lower() == host


def _normalize_path(path: str) -> str:
    path = path.strip() or "/"
    if not path.startswith("/"):
        path = f"/{path}"
    return path.lower()


def _quatvn_url_filter(url: str, host: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc.lower() != host:
        return False
    if parsed.query:
        return False

    path = _normalize_path(parsed.path)
    if path == "/":
        return False

    reserved_segments = {
        "category",
        "danh-muc",
        "feed",
        "page",
        "search",
        "tag",
        "wp-admin",
        "wp-content",
        "wp-includes",
        "wp-json",
    }
    segments = [segment for segment in path.strip("/").split("/") if segment]
    if not segments:
        return False
    if any(segment.lower() in reserved_segments for segment in segments):
        return False

    last_segment = segments[-1]
    if "." in last_segment:
        return False
    return True


def resolve_crawl_preset(start_url: str, site_preset: str = "auto") -> CrawlPreset:
    normalized_start = _normalize_url(start_url, start_url)
    if not normalized_start:
        raise ValueError("Invalid start URL.")

    host = urlparse(normalized_start).netloc.lower()
    presets: dict[str, CrawlPreset] = {
        "generic": CrawlPreset(name="generic", include_substring=""),
        "vlxx": CrawlPreset(name="vlxx", include_substring="/video/"),
        "quatvn": CrawlPreset(
            name="quatvn",
            include_substring="",
            exclude_substrings=(
                "/author/",
                "/danh-muc/",
                "/feed/",
                "/page/",
                "/tag/",
                "/wp-",
            ),
            url_filter=_quatvn_url_filter,
        ),
    }

    if site_preset == "auto":
        if host.startswith("vlxx."):
            return presets["vlxx"]
        if host == "quatvn.my" or host.endswith(".quatvn.my"):
            return presets["quatvn"]
        return presets["generic"]

    if site_preset not in presets:
        raise ValueError(f"Unsupported crawl preset: {site_preset}")
    return presets[site_preset]


def _matches_target_link(
    url: str,
    host: str,
    include_substring: str,
    exclude_substrings: Iterable[str],
    url_filter: Callable[[str, str], bool] | None,
) -> bool:
    if not _same_host(url, host):
        return False
    lowered = url.lower()
    if any(part.lower() in lowered for part in exclude_substrings):
        return False
    if include_substring and include_substring.lower() not in lowered:
        return False
    if url_filter and not url_filter(url, host):
        return False
    return True


def crawl_site_links(
    start_url: str,
    include_substring: str = "",
    max_pages: int = 300,
    timeout: int = 12,
    user_agent: str = "Mozilla/5.0 (compatible; videotrack-crawler/1.0)",
    exclude_substrings: Iterable[str] = (),
    url_filter: Callable[[str, str], bool] | None = None,
) -> CrawlResult:
    normalized_start = _normalize_url(start_url, start_url)
    if not normalized_start:
        raise ValueError("Invalid start URL.")

    host = urlparse(normalized_start).netloc.lower()
    queue: deque[str] = deque([normalized_start])
    enqueued: set[str] = {normalized_start}
    visited: set[str] = set()
    matched: set[str] = set()
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})

    while queue and len(visited) < max_pages:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)

        try:
            response = session.get(current, timeout=timeout, allow_redirects=True)
            content_type = response.headers.get("Content-Type", "").lower()
            if "text/html" not in content_type:
                continue
            html = response.text
        except requests.RequestException:
            continue

        for link in _extract_links(html, current):
            if _matches_target_link(link, host, include_substring, exclude_substrings, url_filter):
                matched.add(link)
            if not _same_host(link, host):
                continue
            if link not in enqueued and link not in visited:
                queue.append(link)
                enqueued.add(link)

    return CrawlResult(matched_urls=sorted(matched), visited_pages=len(visited))


def save_urls_to_csv(urls: Iterable[str], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.writer(csv_file)
        writer.writerow(["url"])
        for url in urls:
            writer.writerow([url])
