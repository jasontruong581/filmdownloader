from __future__ import annotations

import re
from collections import OrderedDict
from urllib.parse import urljoin, urlparse

import requests

from .models import CaptureResult, StreamCandidate

STREAM_PATTERNS: list[tuple[re.Pattern[str], str, int]] = [
    (re.compile(r"\.m3u8($|\?)", re.IGNORECASE), "hls", 100),
    (re.compile(r"\.mpd($|\?)", re.IGNORECASE), "dash", 90),
    (re.compile(r"\.mp4($|\?)", re.IGNORECASE), "mp4", 80),
    (re.compile(r"playlist", re.IGNORECASE), "playlist", 70),
    (re.compile(r"/hls/", re.IGNORECASE), "hls", 70),
    (re.compile(r"/dash/", re.IGNORECASE), "dash", 70),
]

CONTENT_TYPE_HINTS: list[tuple[str, str, int]] = [
    ("application/vnd.apple.mpegurl", "hls", 100),
    ("application/x-mpegurl", "hls", 100),
    ("application/dash+xml", "dash", 90),
    ("video/mp4", "mp4", 80),
]

BLOCKED_URL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\.webmanifest($|\?)", re.IGNORECASE),
    re.compile(r"favicon", re.IGNORECASE),
    re.compile(r"service-worker", re.IGNORECASE),
    re.compile(r"google-analytics|doubleclick|googletagmanager", re.IGNORECASE),
]

BLOCKED_CONTENT_TYPES: tuple[str, ...] = (
    "application/manifest+json",
    "application/json",
    "text/html",
    "text/css",
    "application/javascript",
    "text/javascript",
)

AD_URL_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(^|[/?._-])(ad|ads|advert|promo)([/?._-]|$)", re.IGNORECASE),
    re.compile(r"preroll|pre-roll|midroll|mid-roll|vast", re.IGNORECASE),
]

STREAM_HOST_BONUS_PATTERNS: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"tiktokcdn\.com$", re.IGNORECASE), 18),
    (re.compile(r"bytecdn", re.IGNORECASE), 12),
]


def _match_url(url: str) -> tuple[str, int] | None:
    for pattern, kind, score in STREAM_PATTERNS:
        if pattern.search(url):
            return kind, score
    return None


def _match_content_type(content_type: str | None) -> tuple[str, int] | None:
    if not content_type:
        return None
    lowered = content_type.lower()
    for token, kind, score in CONTENT_TYPE_HINTS:
        if token in lowered:
            return kind, score
    return None


def _is_blocked(url: str, content_type: str | None) -> bool:
    for pattern in BLOCKED_URL_PATTERNS:
        if pattern.search(url):
            return True
    if content_type:
        lowered = content_type.lower()
        if any(token in lowered for token in BLOCKED_CONTENT_TYPES):
            return True
    return False


def _ad_url_penalty(url: str) -> int:
    for pattern in AD_URL_PATTERNS:
        if pattern.search(url):
            return 20
    return 0


def _stream_host_bonus(host: str | None) -> int:
    if not host:
        return 0
    for pattern, bonus in STREAM_HOST_BONUS_PATTERNS:
        if pattern.search(host):
            return bonus
    return 0


def _media_fallback_kind(content_type: str | None) -> tuple[str, int] | None:
    if not content_type:
        return ("media", 55)

    lowered = content_type.lower()
    if lowered.startswith("image/"):
        return None
    if lowered.startswith("video/"):
        if "mp4" in lowered:
            return ("mp4", 75)
        return ("media", 65)
    if "application/octet-stream" in lowered:
        return ("media", 60)

    return ("media", 50)


def build_request_headers(capture: CaptureResult) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": capture.user_agent,
        "Referer": capture.final_url,
    }
    if capture.cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in capture.cookies.items())
    return headers


def extract_embed_urls(capture: CaptureResult) -> list[str]:
    found: OrderedDict[str, None] = OrderedDict()
    for req in capture.requests:
        url = req.url
        parsed = urlparse(url)
        path = (parsed.path or "").lower()
        if not parsed.scheme.startswith("http"):
            continue
        if "/embed/" in path or "/player/" in path:
            found[url] = None
    return list(found.keys())


def _host_allowed(host: str | None, allow_hosts: list[str]) -> bool:
    if not allow_hosts:
        return True
    if not host:
        return False
    h = host.lower()
    for allowed in allow_hosts:
        a = allowed.lower().strip()
        if not a:
            continue
        if h == a or h.endswith(f".{a}"):
            return True
    return False


def filter_candidates_by_host(candidates: list[StreamCandidate], allow_hosts: list[str]) -> list[StreamCandidate]:
    if not allow_hosts:
        return candidates
    return [c for c in candidates if _host_allowed(c.host, allow_hosts)]


def _looks_like_hls(candidate: StreamCandidate) -> bool:
    url = candidate.url.lower()
    return candidate.kind in {"hls", "playlist"} or ".m3u8" in url or "manifest" in url


def _hls_segment_is_image(session: requests.Session, seg_url: str) -> bool:
    try:
        head = session.head(seg_url, timeout=8, allow_redirects=True)
        ctype = (head.headers.get("content-type") or "").lower()
        if ctype.startswith("image/"):
            return True
    except requests.RequestException:
        pass

    try:
        get = session.get(seg_url, timeout=8, stream=True)
        ctype = (get.headers.get("content-type") or "").lower()
        get.close()
        if ctype.startswith("image/"):
            return True
    except requests.RequestException:
        pass

    return False


def precheck_hls_candidates(
    candidates: list[StreamCandidate],
    capture: CaptureResult,
    max_checks: int = 6,
) -> list[StreamCandidate]:
    headers = build_request_headers(capture)
    checked = 0

    with requests.Session() as session:
        session.headers.update(headers)
        for candidate in candidates:
            if checked >= max_checks:
                break
            if not _looks_like_hls(candidate):
                continue
            checked += 1

            try:
                playlist = session.get(candidate.url, timeout=12)
            except requests.RequestException:
                candidate.validation_note = "hls_precheck_request_error"
                candidate.score -= 25
                continue

            if not playlist.ok:
                candidate.validation_note = f"hls_precheck_http_{playlist.status_code}"
                candidate.score -= 25
                continue

            text = playlist.text or ""
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            segments = [line for line in lines if not line.startswith("#")]

            if not segments:
                candidate.validation_note = "hls_precheck_no_segments"
                candidate.score -= 25
                continue

            segment_urls = [urljoin(candidate.url, seg) for seg in segments[:3]]
            image_hit = False
            for seg_url in segment_urls:
                if re.search(r"\.(png|jpg|jpeg|gif|webp)(\?|$)", seg_url, re.IGNORECASE):
                    image_hit = True
                    break
                if _hls_segment_is_image(session, seg_url):
                    image_hit = True
                    break

            if image_hit:
                # Some providers obfuscate HLS segments behind image-like URLs/types.
                # Keep candidate and do not hard-penalize this pattern.
                candidate.validation_note = "hls_precheck_image_like_segment"
                candidate.score += 1
            else:
                candidate.validation_note = "hls_precheck_ok"
                candidate.score += 3

    return sorted(candidates, key=lambda x: x.score, reverse=True)


def detect_candidates(capture: CaptureResult, probe: bool = True) -> list[StreamCandidate]:
    dedup: OrderedDict[str, StreamCandidate] = OrderedDict()

    for req in capture.requests:
        content_type = req.response_headers.get("content-type") or req.response_headers.get("Content-Type")
        if _is_blocked(req.url, content_type):
            continue

        url_match = _match_url(req.url)
        ctype_match = _match_content_type(content_type)

        fallback = None
        if not url_match and not ctype_match:
            if req.resource_type == "Media":
                fallback = _media_fallback_kind(content_type)
            if not fallback:
                continue

        if url_match and ctype_match:
            kind = ctype_match[0] if ctype_match[1] >= url_match[1] else url_match[0]
            score = max(url_match[1], ctype_match[1])
        elif url_match:
            kind, score = url_match
        elif fallback:
            kind, score = fallback
        else:
            kind, score = ctype_match  # type: ignore[assignment]

        if req.resource_type == "Media":
            score += 5

        score -= _ad_url_penalty(req.url)
        score += _stream_host_bonus((urlparse(req.url).hostname or "").lower() or None)

        existing = dedup.get(req.url)
        if existing and existing.score >= score:
            continue

        dedup[req.url] = StreamCandidate(
            url=req.url,
            kind=kind,
            score=score,
            source="network_log",
            status_code=req.status,
            content_type=content_type,
        )

    candidates = sorted(dedup.values(), key=lambda x: x.score, reverse=True)

    if probe and candidates:
        headers = build_request_headers(capture)
        with requests.Session() as session:
            session.headers.update(headers)
            for candidate in candidates[:12]:
                try:
                    response = session.get(candidate.url, timeout=12, stream=True)
                    candidate.status_code = response.status_code
                    candidate.content_type = response.headers.get("content-type", candidate.content_type)
                    response.close()
                    if _is_blocked(candidate.url, candidate.content_type):
                        candidate.score = -1
                        continue

                    ctype_match = _match_content_type(candidate.content_type)
                    if ctype_match:
                        candidate.kind = ctype_match[0]
                        candidate.score = max(candidate.score, ctype_match[1] + 3)
                    else:
                        fallback = _media_fallback_kind(candidate.content_type)
                        if fallback:
                            candidate.kind = fallback[0]
                            candidate.score = max(candidate.score, fallback[1])
                    if response.ok:
                        candidate.score += 2
                except requests.RequestException:
                    continue

    return [c for c in sorted(candidates, key=lambda x: x.score, reverse=True) if c.score >= 0]
