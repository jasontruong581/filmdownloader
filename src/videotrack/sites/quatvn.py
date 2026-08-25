from __future__ import annotations

import re
import subprocess
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import unquote, urljoin, urldefrag, urlparse

import requests

from ..core.capture import _build_driver, _try_play_in_current_context, selenium_api
from ..core.download import _request_headers, _run_command, _safe_name
from ..core.models import CaptureResult, CrawlPreset, StreamCandidate
from ..core.paths import ensure_scratch_dir
from . import BaseSitePlugin, register


def is_quatvn_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "quatvn.my" or host.endswith(".quatvn.my")


def is_quatvn_stream_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    return host == "quatvn2.net" and path.startswith("/stream/") and path.endswith(".webp")


def collection_slug_token(page_url: str) -> str:
    path = (urlparse(page_url).path or "").strip("/")
    slug = path.split("/")[-1] if path else ""
    slug = re.sub(r"-collection$", "", slug, flags=re.IGNORECASE)
    slug = slug.replace("-", " ").replace("_", " ").strip().lower()
    return slug


def _quatvn_stream_parts(url: str) -> tuple[str, int | None]:
    path = urlparse(url).path or ""
    name = unquote(path.rsplit("/", 1)[-1])
    match = re.search(r"^(?P<prefix>.+?)\s*\((?P<num>\d+)\)\.webp$", name, re.IGNORECASE)
    if match:
        prefix = match.group("prefix").strip().lower()
        return prefix, int(match.group("num"))
    return Path(name).stem.strip().lower(), None


def _quatvn_stream_sort_key(url: str) -> tuple[str, int, str]:
    prefix, number = _quatvn_stream_parts(url)
    path = urlparse(url).path or ""
    decoded = unquote(path.rsplit("/", 1)[-1]).lower()
    return prefix, number or 0, decoded


def _stream_matches_collection(url: str, page_url: str) -> bool:
    prefix, number = _quatvn_stream_parts(url)
    if number is None:
        return False
    token = collection_slug_token(page_url)
    if not token:
        return True
    token_parts = [part for part in token.split() if part]
    prefix_parts = [part for part in prefix.split() if part]
    return bool(token_parts) and token_parts == prefix_parts


def _is_generic_quatvn_path(path: str) -> bool:
    slug = (path or "").strip("/").lower()
    if not slug:
        return True
    if "/" in slug:
        return False

    generic_slugs = {
        "cn",
        "hot",
        "jp",
        "kr",
        "my",
        "th",
        "top-10",
        "trending",
        "us",
        "phim-sex-vn",
    }
    if slug in generic_slugs:
        return True
    if re.fullmatch(r"[a-z]{2}", slug):
        return True
    return False


def _score_quatvn_target_href(href: str, page_url: str, scope_text: str, media_src: str) -> int:
    parsed = urlparse(href)
    page_host = (urlparse(page_url).hostname or "").lower()
    host = (parsed.hostname or "").lower()
    if host != page_host:
        return -999

    path = (parsed.path or "").strip("/")
    if not path or href == page_url:
        return -999
    if _is_generic_quatvn_path(path):
        return -999
    if path.endswith("-collection"):
        return -999

    token = collection_slug_token(page_url)
    href_slug = path.split("/")[-1].replace("-", " ").replace("_", " ").lower()
    media_prefix, media_number = _quatvn_stream_parts(media_src)
    text = (scope_text or "").lower()

    score = 0
    if token and token in href_slug:
        score += 100
    if token and token in text:
        score += 40
    if media_prefix and media_prefix in href_slug:
        score += 90
    if media_prefix and media_prefix in text:
        score += 30
    if media_number is not None and str(media_number) in href_slug:
        score += 25
    if re.search(r"\d{4,}", href_slug):
        score += 10
    if path.count("/") <= 1:
        score += 5

    return score


def extract_quatvn_stream_candidates(capture: CaptureResult, page_url: str | None = None) -> list[StreamCandidate]:
    dedup: OrderedDict[str, StreamCandidate] = OrderedDict()

    for req in capture.requests:
        if not is_quatvn_stream_url(req.url):
            continue
        if page_url and not _stream_matches_collection(req.url, page_url):
            continue
        dedup[req.url] = StreamCandidate(
            url=req.url,
            kind="quatvn_webp",
            score=80,
            source="quatvn_stream",
            status_code=req.status,
            content_type=req.response_headers.get("content-type") or req.response_headers.get("Content-Type"),
        )

    return sorted(dedup.values(), key=lambda item: _quatvn_stream_sort_key(item.url))


def _normalize_http_url(raw_url: str, base_url: str) -> str | None:
    if not raw_url:
        return None
    resolved = urljoin(base_url, raw_url.strip())
    cleaned, _ = urldefrag(resolved)
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        return None
    return parsed.geturl()


def _extract_media_like_urls(driver, base_url: str) -> list[str]:
    raw_urls = driver.execute_script(
        """
        const selectors = ['iframe', 'video', 'source', 'embed'];
        const attrs = ['src', 'data-src', 'data-lazy-src', 'data-url', 'data-iframe'];
        const found = [];

        for (const selector of selectors) {
          for (const node of document.querySelectorAll(selector)) {
            for (const attr of attrs) {
              const value = node.getAttribute(attr);
              if (value) found.push(value);
            }
          }
        }

        for (const node of document.querySelectorAll('[data-settings], [data-player], [data-config], script')) {
          const text = node.getAttribute('data-settings')
            || node.getAttribute('data-player')
            || node.getAttribute('data-config')
            || node.textContent
            || '';
          const matches = text.match(/https?:\\/\\/[^"'\\s<>()]+/g) || [];
          for (const item of matches) found.push(item);
        }

        return found;
        """
    )

    normalized: OrderedDict[str, None] = OrderedDict()
    for item in raw_urls or []:
        normalized_url = _normalize_http_url(str(item), base_url)
        if not normalized_url:
            continue
        normalized[normalized_url] = None
    return list(normalized.keys())


def _filter_candidate_target(url: str, page_url: str) -> bool:
    lowered = url.lower()
    page_host = (urlparse(page_url).hostname or "").lower()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()

    if url == page_url:
        return False
    if any(token in lowered for token in (".css", ".js", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp")):
        return False
    if any(token in lowered for token in ("google-analytics", "doubleclick", "googletagmanager", "facebook.com")):
        return False

    if any(token in lowered for token in ("/embed/", "/player/", "jwplayer", "stream", ".m3u8", ".mpd", ".mp4")):
        return True

    if host and host != page_host and any(token in lowered for token in ("video", "embed", "play", "player")):
        return True

    return False


def _collect_tab_elements(driver):
    selectors = ",".join(
        [
            '[role="tab"]',
            '[data-bs-toggle="tab"]',
            '[data-toggle="tab"]',
            '[aria-controls]',
            '.nav-tabs a',
            '.nav-tabs button',
            '.tabs a',
            '.tabs button',
            '.tab a',
            '.tab button',
            '.elementor-tab-title',
        ]
    )
    elements = driver.find_elements(selenium_api().By.CSS_SELECTOR, selectors)
    unique = []
    seen: set[str] = set()

    for element in elements:
        try:
            href = (element.get_attribute("href") or "").strip()
            text = (element.text or "").strip()
            role = (element.get_attribute("role") or "").strip().lower()
            aria_controls = (element.get_attribute("aria-controls") or "").strip()
            data_toggle = (element.get_attribute("data-toggle") or element.get_attribute("data-bs-toggle") or "").strip()
        except Exception:
            continue

        if href and not href.startswith("#") and role != "tab" and not data_toggle and not aria_controls:
            continue

        key = "|".join([href, text, role, aria_controls, data_toggle])
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(element)

    return unique


def discover_quatvn_targets(
    page_url: str,
    wait_seconds: int = 12,
    headless: bool = True,
    tab_pause_seconds: float = 1.0,
) -> list[str]:
    driver = _build_driver(headless=headless)
    discovered: OrderedDict[str, None] = OrderedDict()

    try:
        driver.get(page_url)
        api = selenium_api()
        api.WebDriverWait(driver, 20).until(
            api.EC.presence_of_element_located((api.By.TAG_NAME, "body"))
        )

        _try_play_in_current_context(driver)

        linked_targets = driver.execute_script(
            """
            const out = [];
            const srcAttrs = ['src', 'data-src', 'data-lazy-src', 'data-url'];
            const nodes = Array.from(document.querySelectorAll('img, source, video'));
            const containerSelectors = [
              '.snax-figure',
              '.snax-item',
              '.snax-item-card',
              '.g1-collection-item',
              '.g1-collection',
              'article',
              'figure',
              'li',
              '.swiper-slide',
              '.flickity-slider > *'
            ];

            for (const node of nodes) {
              let mediaSrc = '';
              for (const attr of srcAttrs) {
                const value = node.getAttribute(attr);
                if (value) { mediaSrc = value; break; }
              }
              if (!mediaSrc && node.currentSrc) mediaSrc = node.currentSrc;
              if (!mediaSrc) continue;

              let container = node;
              for (let i = 0; i < 8 && container; i += 1) {
                const matches = container.matches && containerSelectors.some((selector) => container.matches(selector));
                if (matches) break;
                container = container.parentElement;
              }

              const hrefs = [];
              const scope = container || node.parentElement || document;
              const anchors = Array.from(scope.querySelectorAll ? scope.querySelectorAll('a[href]') : []);
              for (const anchor of anchors) {
                const href = anchor.href || anchor.getAttribute('href') || '';
                if (href) hrefs.push(href);
              }

              if (!hrefs.length) {
                let parent = node;
                for (let i = 0; i < 8 && parent; i += 1) {
                  if (parent.tagName && parent.tagName.toLowerCase() === 'a') {
                    const href = parent.href || parent.getAttribute('href') || '';
                    if (href) hrefs.push(href);
                    break;
                  }
                  parent = parent.parentElement;
                }
              }

              out.push({
                mediaSrc,
                hrefs,
                text: (scope && scope.textContent ? scope.textContent : '').slice(0, 300),
              });
            }
            return out;
            """
        )

        linked_urls: OrderedDict[str, None] = OrderedDict()
        matched_media_count = 0
        for item in linked_targets or []:
            media_src = _normalize_http_url(str(item.get("mediaSrc") or ""), driver.current_url)
            hrefs = item.get("hrefs") or []
            scope_text = str(item.get("text") or "")
            if not media_src:
                continue
            if not is_quatvn_stream_url(media_src):
                continue
            if not _stream_matches_collection(media_src, page_url):
                continue
            matched_media_count += 1
            best_href = None
            best_score = -999
            for raw_href in hrefs:
                href = _normalize_http_url(str(raw_href or ""), driver.current_url)
                if not href:
                    continue
                score = _score_quatvn_target_href(href, page_url, scope_text, media_src)
                if score > best_score:
                    best_score = score
                    best_href = href
            if best_href and best_score >= 60:
                linked_urls[best_href] = None

        if linked_urls and (matched_media_count == 0 or len(linked_urls) >= max(3, int(matched_media_count * 0.7))):
            return list(linked_urls.keys())

        for url in _extract_media_like_urls(driver, driver.current_url):
            if _filter_candidate_target(url, driver.current_url) and (not is_quatvn_stream_url(url) or _stream_matches_collection(url, page_url)):
                discovered[url] = None

        tab_elements = _collect_tab_elements(driver)
        for element in tab_elements[:40]:
            try:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
                driver.execute_script("arguments[0].click();", element)
                time.sleep(tab_pause_seconds)
                _try_play_in_current_context(driver)
            except Exception:
                continue

            for url in _extract_media_like_urls(driver, driver.current_url):
                if _filter_candidate_target(url, driver.current_url) and (not is_quatvn_stream_url(url) or _stream_matches_collection(url, page_url)):
                    discovered[url] = None

        if not discovered:
            time.sleep(max(wait_seconds, 1))
            _try_play_in_current_context(driver)
            for url in _extract_media_like_urls(driver, driver.current_url):
                if _filter_candidate_target(url, driver.current_url) and (not is_quatvn_stream_url(url) or _stream_matches_collection(url, page_url)):
                    discovered[url] = None
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    return list(discovered.keys())


# --- Stream asset conversion -------------------------------------------------
#
# This site serves animated WebP sequences rather than a normal stream, so the
# frames are extracted with ImageMagick and re-encoded. Core knows nothing about
# it: the plugin claims the candidate kind and does the work.


def asset_suffix(url: str) -> str:
    """Per-asset filename suffix, so clips of one page do not overwrite."""
    path = urlparse(url).path or ""
    name = unquote(path.rsplit("/", 1)[-1])
    match = re.search(r"\((\d+)\)\.webp$", name, re.IGNORECASE)
    if match:
        return f"clip-{int(match.group(1)):02d}"
    return _safe_name(Path(name).stem or "clip")


def _download_to_temp_file(capture: CaptureResult, candidate: StreamCandidate, suffix: str) -> Path:
    headers = _request_headers(capture, capture.final_url or capture.page_url)
    response = requests.get(candidate.url, headers=headers, timeout=60, stream=True)
    if not response.ok:
        raise RuntimeError(f"asset request failed: HTTP {response.status_code}")

    temp_path = ensure_scratch_dir() / f"quatvn_asset_{abs(hash(candidate.url))}{suffix}"
    with temp_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 512):
            if chunk:
                handle.write(chunk)
    response.close()
    return temp_path


def _frame_delays(temp_in: Path) -> list[float]:
    identify = subprocess.run(
        ["magick", "identify", "-format", "%T\n", str(temp_in)],
        capture_output=True,
        text=True,
    )
    if identify.returncode != 0:
        raise RuntimeError("magick failed to read quatvn webp frame delays")

    delays: list[float] = []
    for line in (identify.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            delays.append(max(float(line) / 100.0, 0.04))
        except ValueError:
            delays.append(0.04)
    return delays


def _write_concat_file(frames: list[Path], delays: list[float], concat_path: Path) -> None:
    lines: list[str] = []
    for index, frame in enumerate(frames):
        lines.append("file " + repr(frame.resolve().as_posix()))
        if index < len(frames) - 1:
            duration = delays[index] if index < len(delays) else 0.04
            lines.append(f"duration {duration:.3f}")
    lines.append("file " + repr(frames[-1].resolve().as_posix()))
    concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def convert_stream_asset(capture: CaptureResult, candidate: StreamCandidate, out_file: Path) -> Path:
    """Turn one animated WebP asset into a playable MP4."""
    temp_in = _download_to_temp_file(capture, candidate, ".webp")
    out_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="quatvn_frames_", dir=str(ensure_scratch_dir())) as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            frame_pattern = temp_dir / "frame_%05d.png"
            concat_path = temp_dir / "frames.txt"

            if _run_command(["magick", str(temp_in), "-coalesce", str(frame_pattern)]) != 0:
                raise RuntimeError("magick failed to extract quatvn webp frames")

            delays = _frame_delays(temp_in)
            frames = sorted(temp_dir.glob("frame_*.png"))
            if not frames:
                raise RuntimeError("no frames extracted from quatvn webp asset")

            _write_concat_file(frames, delays, concat_path)

            encode_cmd = [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "info",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_path),
                "-fps_mode",
                "vfr",
                "-pix_fmt",
                "yuv420p",
                "-an",
                str(out_file),
            ]
            if _run_command(encode_cmd) != 0:
                raise RuntimeError("ffmpeg failed to encode quatvn webp frames")
    finally:
        try:
            temp_in.unlink(missing_ok=True)
        except Exception:
            pass

    if not out_file.exists() or out_file.stat().st_size == 0:
        try:
            out_file.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError("quatvn conversion produced empty output")

    return out_file


def _quatvn_crawl_url_filter(url: str, host: str) -> bool:
    parsed = urlparse(url)
    if parsed.netloc.lower() != host or parsed.query:
        return False

    path = (parsed.path or "/").lower()
    if path == "/":
        return False

    reserved = {
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
    if not segments or any(segment in reserved for segment in segments):
        return False
    return "." not in segments[-1]


class QuatvnPlugin(BaseSitePlugin):
    """Site whose media arrives as numbered animated WebP assets."""

    name = "quatvn"

    def handles(self, url: str) -> bool:
        return is_quatvn_url(url)

    def claims_kind(self, kind: str) -> bool:
        return kind == "quatvn_webp"

    def output_base(self, candidate: StreamCandidate, capture: CaptureResult, base: str | None) -> str | None:
        if candidate.kind != "quatvn_webp":
            return None
        root = base or _safe_name(capture.title or "quatvn")
        return f"{root}-{asset_suffix(candidate.url)}"

    def postprocess(self, capture: CaptureResult, candidate: StreamCandidate, out_file: Path) -> Path | None:
        if candidate.kind != "quatvn_webp":
            return None
        return convert_stream_asset(capture, candidate, out_file)

    def crawl_preset(self) -> CrawlPreset:
        return CrawlPreset(
            name="quatvn",
            include_substring="",
            exclude_substrings=("/author/", "/danh-muc/", "/feed/", "/page/", "/tag/", "/wp-"),
            url_filter=_quatvn_crawl_url_filter,
        )


register(QuatvnPlugin())
