from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

from ..core.download import download_with_ffmpeg
from ..core.models import BatchItem, BatchProbe, CaptureResult, StreamCandidate
from ..core.resolvers import DEFAULT_USER_AGENT, media_kind
from . import BaseSitePlugin, register


@dataclass(frozen=True)
class CollectionVideo:
    title: str
    source_url: str


@dataclass(frozen=True)
class Collection:
    source_url: str
    title: str
    slug: str
    videos: tuple[CollectionVideo, ...]
    cookies: dict[str, str] = field(default_factory=dict)


def _safe_segment(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", " ", value)
    value = re.sub(r"\s+", " ", value).strip().rstrip(". ")
    return value[:120] or "collection"


def _collection_title(page_html: str, fallback: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
    if not match:
        return fallback
    value = html.unescape(re.sub(r"<[^>]+>", " ", match.group(1))).split("•", 1)[0].strip()
    return re.sub(r"\s+", " ", value) or fallback


def parse_flowplayer_collection(page_html: str, source_url: str) -> Collection:
    """Parse the HTML-encoded Flowplayer data-item entries embedded in a collection page."""
    seen: set[str] = set()
    videos: list[CollectionVideo] = []
    for match in re.finditer(r'data-item="([^"]+)"', page_html):
        try:
            item = json.loads(html.unescape(match.group(1)))
        except json.JSONDecodeError:
            continue
        source = next(
            (entry.get("src") for entry in item.get("sources", []) if isinstance(entry, dict) and isinstance(entry.get("src"), str)),
            None,
        )
        if not source or source in seen:
            continue
        seen.add(source)
        fallback = Path(urlparse(source).path).name or f"video-{len(videos) + 1}"
        title = item.get("fv_title") if isinstance(item.get("fv_title"), str) else fallback
        videos.append(CollectionVideo(title=title.strip() or fallback, source_url=source))

    fallback_slug = Path(urlparse(source_url).path.rstrip("/")).name or "collection"
    title = _collection_title(page_html, fallback_slug)
    return Collection(source_url=source_url, title=title, slug=_safe_segment(fallback_slug), videos=tuple(videos))


def fetch_collection(url: str, timeout: int = 20) -> Collection:
    session = requests.Session()
    session.headers.update({"User-Agent": DEFAULT_USER_AGENT})
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    collection = parse_flowplayer_collection(response.text or "", response.url)
    return Collection(
        source_url=collection.source_url,
        title=collection.title,
        slug=collection.slug,
        videos=collection.videos,
        cookies={cookie.name: cookie.value for cookie in session.cookies},
    )


def _destination_for(video: CollectionVideo, position: int, output_dir: Path) -> Path:
    extension = Path(urlparse(video.source_url).path).suffix.lower()
    extension = ".mp4" if extension in {"", ".m3u8", ".mpd"} else extension
    title = _safe_segment(re.sub(r"\.[A-Za-z0-9]+$", "", video.title))
    return output_dir / f"{position:02d} - {title}{extension}"


def _existing_file_matches_source(
    video: CollectionVideo,
    destination: Path,
    referer: str,
    cookies: dict[str, str],
) -> bool:
    if not destination.exists() or destination.stat().st_size == 0:
        return False
    source_extension = Path(urlparse(video.source_url).path).suffix.lower()
    if source_extension in {".m3u8", ".mpd"}:
        return False
    try:
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Referer": referer}
        if cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
        response = requests.head(
            video.source_url,
            headers=headers,
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
        expected_size = response.headers.get("content-length")
        return expected_size is not None and destination.stat().st_size == int(expected_size)
    except (ValueError, requests.RequestException):
        return False


def download_collection(collection: Collection, output_dir: Path, dry_run: bool, overwrite: bool) -> dict:
    base_dir = output_dir / collection.slug
    base_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "source": collection.source_url,
        "collection_name": collection.title,
        "expected_count": len(collection.videos),
        "videos": [],
    }
    capture = CaptureResult(
        page_url=collection.source_url,
        final_url=collection.source_url,
        title=collection.title,
        user_agent=DEFAULT_USER_AGENT,
        cookies=collection.cookies,
        requests=[],
    )
    manifest_path = base_dir / "manifest.json"
    for position, video in enumerate(collection.videos, start=1):
        destination = _destination_for(video, position, base_dir)
        item = {
            "index": position,
            "title": video.title,
            "source_url": video.source_url,
            "destination": str(destination),
            "status": "pending",
        }
        manifest["videos"].append(item)
        if dry_run:
            item["status"] = "dry_run"
        elif not overwrite and _existing_file_matches_source(video, destination, collection.source_url, collection.cookies):
            item["status"] = "skipped_existing"
        else:
            candidate = StreamCandidate(
                url=video.source_url,
                kind=media_kind(video.source_url),
                score=100,
                source="flowplayer_collection",
                referer=collection.source_url,
            )
            temporary = destination.with_name(f"{destination.stem}.part{destination.suffix}")
            try:
                temporary.unlink(missing_ok=True)
                download_with_ffmpeg(capture, candidate, base_dir, output_file=temporary, write_metadata=False)
                if not temporary.exists() or temporary.stat().st_size == 0:
                    raise RuntimeError("download produced an empty output")
                temporary.replace(destination)
                item["status"] = "downloaded"
                item["size"] = destination.stat().st_size
            except Exception as exc:  # noqa: BLE001
                temporary.unlink(missing_ok=True)
                item["status"] = "failed"
                item["error"] = str(exc)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


class FlowplayerPlugin(BaseSitePlugin):
    """Pages that embed a Flowplayer collection as HTML-encoded data-item entries.

    `handles` stays False: this pattern is identified by page markup, not by
    hostname, and a URL prefilter must not fetch anything to find out. The plugin
    contributes through batch probing and the `collect` command instead of
    through URL routing.
    """

    name = "flowplayer"

    def handles(self, url: str) -> bool:
        return False

    def probe_batch(self, url: str) -> BatchProbe | None:
        """Count the collection entries this page embeds. One request."""
        try:
            collection = fetch_collection(url)
        except requests.RequestException:
            return None

        if len(collection.videos) < 2:
            return None

        return BatchProbe(
            capability="collection",
            confidence="proven",
            items=tuple(
                BatchItem(url=video.source_url, title=video.title) for video in collection.videos
            ),
            total_estimate=len(collection.videos),
            truncated=False,
            reason="",
        )


register(FlowplayerPlugin())
