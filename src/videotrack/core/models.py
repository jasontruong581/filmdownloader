from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable
from urllib.parse import urlparse


@dataclass
class NetworkRequest:
    url: str
    method: str
    headers: dict[str, str]
    resource_type: str | None = None
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class CaptureResult:
    page_url: str
    final_url: str
    title: str
    user_agent: str
    cookies: dict[str, str]
    requests: list[NetworkRequest]

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_url": self.page_url,
            "final_url": self.final_url,
            "title": self.title,
            "user_agent": self.user_agent,
            "cookies": self.cookies,
            "requests": [
                {
                    "url": req.url,
                    "method": req.method,
                    "headers": req.headers,
                    "resource_type": req.resource_type,
                    "status": req.status,
                    "response_headers": req.response_headers,
                }
                for req in self.requests
            ],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "CaptureResult":
        requests = [
            NetworkRequest(
                url=req["url"],
                method=req.get("method", "GET"),
                headers=req.get("headers", {}),
                resource_type=req.get("resource_type"),
                status=req.get("status"),
                response_headers=req.get("response_headers", {}),
            )
            for req in data.get("requests", [])
        ]
        return CaptureResult(
            page_url=data["page_url"],
            final_url=data.get("final_url", data["page_url"]),
            title=data.get("title", ""),
            user_agent=data.get("user_agent", ""),
            cookies=data.get("cookies", {}),
            requests=requests,
        )


@dataclass
class StreamCandidate:
    url: str
    kind: str
    score: int
    source: str
    status_code: int | None = None
    content_type: str | None = None
    host: str | None = None
    probe_duration: float | None = None
    probe_bitrate: int | None = None
    validation_note: str | None = None
    referer: str | None = None

    def __post_init__(self) -> None:
        if self.host is None:
            self.host = (urlparse(self.url).hostname or "").lower() or None

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "kind": self.kind,
            "score": self.score,
            "source": self.source,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "host": self.host,
            "probe_duration": self.probe_duration,
            "probe_bitrate": self.probe_bitrate,
            "validation_note": self.validation_note,
            "referer": self.referer,
        }


@dataclass
class PageMetadata:
    """Descriptive fields a site plugin can extract from a page.

    Neutral by design: a plugin fills what its site exposes and leaves the rest
    as None. Core naming falls back to the page title and then the URL path.
    """

    video_code: str | None = None
    title: str | None = None
    actresses: list[str] | None = None
    description: str | None = None


@dataclass(frozen=True)
class CrawlPreset:
    """Per-site rules for discovering child URLs on one host."""

    name: str
    include_substring: str
    exclude_substrings: tuple[str, ...] = ()
    url_filter: Callable[[str, str], bool] | None = None


@dataclass(frozen=True)
class BatchItem:
    """One enumerated entry of a multi-item page."""

    url: str
    title: str = ""


@dataclass(frozen=True)
class BatchProbe:
    """The result of asking whether a URL enumerates more than one item.

    A probe proves *enumeration*, never *downloadability*. `confidence` is
    "proven" only when concrete items were listed, so the UI can show them before
    enabling anything; "possible" means links were found but no media was
    confirmed; "none" carries a specific human-readable `reason`.
    """

    capability: str = "none"
    confidence: str = "none"
    items: tuple[BatchItem, ...] = ()
    total_estimate: int | None = None
    truncated: bool = False
    reason: str = ""

    @property
    def is_batchable(self) -> bool:
        return self.confidence in {"proven", "possible"} and len(self.items) >= 2


def _human_size(num_bytes: int | None) -> str | None:
    if not num_bytes or num_bytes <= 0:
        return None
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in {"B", "KB"} else f"{size:.1f} {unit}"
        size /= 1024
    return None


def _bitrate_label(tbr_kbps: float) -> str:
    """Format a bitrate in the unit a reader expects at that magnitude."""
    if tbr_kbps < 1000:
        return f"{tbr_kbps:.0f} kbps"
    return f"{tbr_kbps / 1000:.1f} Mbps"


@dataclass(frozen=True)
class MediaFormat:
    """One selectable rendition of a resolved item.

    Every measurement is optional on purpose: extractors routinely omit filesize,
    bitrate, or even resolution, and a UI that renders a missing number as zero
    lies about the content.
    """

    format_id: str
    container: str | None = None
    height: int | None = None
    width: int | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    filesize: int | None = None
    filesize_approx: int | None = None
    tbr: float | None = None
    note: str | None = None

    @property
    def has_video(self) -> bool:
        return self.vcodec not in {None, "", "none"}

    @property
    def has_audio(self) -> bool:
        return self.acodec not in {None, "", "none"}

    @property
    def track(self) -> str:
        """Which of the three selectable groups this format belongs to."""
        if self.has_video and self.has_audio:
            return "both"
        if self.has_video:
            return "video-only"
        if self.has_audio:
            return "audio-only"
        return "unknown"

    @property
    def best_effort_size(self) -> int | None:
        return self.filesize or self.filesize_approx

    def resolution_label(self) -> str:
        if self.height:
            return f"{self.height}p"
        if self.width:
            return f"{self.width}w"
        return "audio" if self.track == "audio-only" else "unknown"

    def label(self) -> str:
        """Human-readable summary. Only known facts appear."""
        parts = [self.resolution_label()]
        if self.fps and self.fps >= 50:
            parts[0] = f"{parts[0]}{self.fps:.0f}"
        if self.container:
            parts.append(self.container)
        if self.tbr:
            parts.append(_bitrate_label(self.tbr))
        size = _human_size(self.best_effort_size)
        if size:
            parts.append(size)
        if self.track == "video-only":
            parts.append("video only")
        elif self.track == "audio-only" and self.height is None:
            parts.append("audio only")
        return " / ".join(parts)

    def sort_key(self) -> tuple[int, int, float, float]:
        """Best first: complete streams, then resolution, then bitrate."""
        track_rank = {"both": 2, "video-only": 1, "audio-only": 0}.get(self.track, 0)
        return (track_rank, self.height or 0, self.tbr or 0.0, float(self.best_effort_size or 0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_id": self.format_id,
            "container": self.container,
            "height": self.height,
            "width": self.width,
            "fps": self.fps,
            "vcodec": self.vcodec,
            "acodec": self.acodec,
            "filesize": self.filesize,
            "filesize_approx": self.filesize_approx,
            "tbr": self.tbr,
            "note": self.note,
            "track": self.track,
            "label": self.label(),
        }


def sort_formats(formats: Iterable[MediaFormat]) -> tuple[MediaFormat, ...]:
    return tuple(sorted(formats, key=lambda item: item.sort_key(), reverse=True))
