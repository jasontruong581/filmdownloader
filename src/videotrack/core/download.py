"""Download a selected candidate through FFmpeg.

Site-neutral. Metadata extraction, per-site filename quirks, and asset
conversion are asked of the site registry rather than implemented here, so a
universal download does not get named from selectors that only exist on one
site.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .models import CaptureResult, PageMetadata, StreamCandidate

FFMPEG_STDERR_TAIL_LINES = 12


def _safe_name(raw: str) -> str:
    value = raw.strip() or "video"
    value = re.sub(r"[\\/:*?\"<>|]", "_", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120]


def _extract_page_id(url: str) -> str | None:
    try:
        path = urlparse(url).path or ""
    except Exception:
        return None
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None
    tail = parts[-1]
    if re.fullmatch(r"\d+", tail):
        return tail
    return None


def _request_headers(capture: CaptureResult, referer: str) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": capture.user_agent,
        "Referer": referer,
    }
    if capture.cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in capture.cookies.items())
    return headers


def _site_plugin_for(url: str):
    """Look the registry up lazily to keep core free of a site-package import."""
    from ..sites import plugin_for

    return plugin_for(url)


def _site_plugin_for_kind(kind: str):
    from ..sites import plugin_for_kind

    return plugin_for_kind(kind)


def fetch_page_metadata(capture: CaptureResult, page_html: str | None = None) -> PageMetadata:
    """Descriptive fields from whichever site plugin claims this page."""
    plugin = _site_plugin_for(capture.final_url or capture.page_url)
    if plugin is None:
        return PageMetadata()
    try:
        return plugin.metadata(capture, page_html) or PageMetadata()
    except Exception:  # noqa: BLE001 - naming must never fail a download
        return PageMetadata()


def unique_path(path: Path) -> Path:
    """Return `path`, or the first free `name (n).ext` beside it.

    Two pages can easily derive the same name. FFmpeg runs with `-y`, so without
    this the second download silently overwrites the first, and with concurrent
    jobs both could write the same file at once.
    """
    if not path.exists():
        return path
    for counter in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not find a free filename beside {path}")


def output_path_for(capture: CaptureResult, output_dir: Path, preferred_base: str | None = None) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    if preferred_base:
        base = _safe_name(preferred_base)
    elif capture.title:
        base = _safe_name(capture.title)
    else:
        parsed = urlparse(capture.final_url)
        base = _safe_name(Path(parsed.path).name or "video")

    if not base.lower().endswith(".mp4"):
        base = f"{base}.mp4"

    return output_dir / base


def _headers_block(capture: CaptureResult, referer: str | None = None) -> str:
    parts: list[str] = []
    if referer or capture.final_url:
        parts.append(f"Referer: {referer or capture.final_url}")
    if capture.cookies:
        cookie_value = "; ".join(f"{k}={v}" for k, v in capture.cookies.items())
        parts.append(f"Cookie: {cookie_value}")
    return "\r\n".join(parts) + "\r\n" if parts else ""


def build_ffmpeg_command(
    capture: CaptureResult,
    candidate: StreamCandidate,
    out_file: Path,
) -> list[str]:
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "info",
    ]

    if capture.user_agent:
        cmd.extend(["-user_agent", capture.user_agent])

    headers = _headers_block(capture, candidate.referer)
    if headers:
        cmd.extend(["-headers", headers])

    candidate_url = candidate.url.lower()
    if candidate.kind in {"hls", "playlist"} or ".m3u8" in candidate_url or "manifest" in candidate_url:
        cmd.extend(
            [
                "-protocol_whitelist",
                "file,http,https,tcp,tls,crypto,data",
                "-allowed_extensions",
                "ALL",
            ]
        )

    cmd.extend(
        [
            "-i",
            candidate.url,
            "-c",
            "copy",
            str(out_file),
        ]
    )
    return cmd


class ToolNotFound(RuntimeError):
    """An external binary is not on PATH or at its configured location."""


def _run_command(cmd: list[str]) -> int:
    """Run an external tool, letting its output through to the terminal.

    A missing binary raises FileNotFoundError rather than returning non-zero, so
    it is translated into a message that names the tool.
    """
    try:
        return subprocess.run(cmd, capture_output=False, text=True).returncode
    except FileNotFoundError as exc:
        raise ToolNotFound(f"{cmd[0]} not found. Install it or set its location in settings.") from exc


def _run_ffmpeg_captured(cmd: list[str]) -> tuple[int, str]:
    """Run FFmpeg, keeping the tail of stderr so a failure can be explained."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ToolNotFound(f"{cmd[0]} not found. Install it or set its location in settings.") from exc

    stderr = proc.stderr or ""
    if stderr:
        print(stderr, end="" if stderr.endswith("\n") else "\n")
    tail = "\n".join(stderr.strip().splitlines()[-FFMPEG_STDERR_TAIL_LINES:])
    return proc.returncode, tail


def _preferred_base(
    metadata_source: CaptureResult,
    metadata: PageMetadata,
    capture: CaptureResult,
    candidate: StreamCandidate,
) -> str | None:
    page_id = _extract_page_id(metadata_source.final_url or metadata_source.page_url)
    base = metadata.video_code or None
    if page_id and metadata.video_code:
        base = f"{page_id}-{metadata.video_code}"

    plugin = _site_plugin_for_kind(candidate.kind)
    if plugin is not None:
        try:
            refined = plugin.output_base(candidate, metadata_source, base)
        except Exception:  # noqa: BLE001
            refined = None
        if refined:
            return refined
    return base


def _write_sidecar(capture: CaptureResult, out_file: Path, metadata: PageMetadata) -> None:
    plugin = _site_plugin_for(capture.final_url or capture.page_url)
    if plugin is None:
        return
    try:
        plugin.write_sidecar(out_file, metadata)
    except Exception:  # noqa: BLE001 - a sidecar is never worth failing a download
        pass


def download_with_ffmpeg(
    capture: CaptureResult,
    candidate: StreamCandidate,
    output_dir: Path,
    metadata_capture: CaptureResult | None = None,
    output_file: Path | None = None,
    write_metadata: bool = True,
) -> Path:
    metadata_source = metadata_capture or capture
    metadata = fetch_page_metadata(metadata_source) if write_metadata else PageMetadata()
    preferred_base = _preferred_base(metadata_source, metadata, capture, candidate)

    out_file = output_file or unique_path(
        output_path_for(capture, output_dir, preferred_base=preferred_base)
    )
    out_file.parent.mkdir(parents=True, exist_ok=True)

    postprocessor = _site_plugin_for_kind(candidate.kind)
    if postprocessor is not None:
        converted = postprocessor.postprocess(capture, candidate, out_file)
        if converted is not None:
            if write_metadata:
                _write_sidecar(metadata_source, converted, metadata)
            return converted

    cmd = build_ffmpeg_command(capture, candidate, out_file)
    returncode, stderr_tail = _run_ffmpeg_captured(cmd)

    if returncode != 0:
        candidate_url = candidate.url.lower()
        if candidate.kind in {"hls", "playlist"} or ".m3u8" in candidate_url or "manifest" in candidate_url:
            out = download_obfuscated_hls(capture, candidate, out_file)
            if write_metadata:
                _write_sidecar(metadata_source, out, metadata)
            return out
        detail = f": {stderr_tail}" if stderr_tail else ""
        raise RuntimeError(f"ffmpeg failed with exit code {returncode}{detail}")

    if write_metadata:
        _write_sidecar(metadata_source, out_file, metadata)
    return out_file


def _extract_png_tail_payload(raw: bytes) -> bytes:
    png_sig = b"\x89PNG\r\n\x1a\n"
    if not raw.startswith(png_sig):
        return raw

    idx = raw.find(b"IEND")
    if idx < 4:
        return raw

    end = idx + 8
    if end >= len(raw):
        return raw
    return raw[end:]


def _is_mpeg_ts(data: bytes) -> bool:
    if len(data) < 188:
        return False
    if data[0] != 0x47:
        return False

    hits = 0
    checks = min(len(data) // 188, 12)
    for i in range(1, checks):
        if data[i * 188] == 0x47:
            hits += 1
    return hits >= max(2, checks - 3)


def download_obfuscated_hls(capture: CaptureResult, candidate: StreamCandidate, out_file: Path) -> Path:
    """Repack an HLS stream whose segments are wrapped behind a PNG header.

    Host-neutral: the technique is detected from the payload, not from a domain.
    """
    manifest_headers = _request_headers(capture, candidate.referer or capture.final_url)
    response = requests.get(candidate.url, headers=manifest_headers, timeout=30)
    if not response.ok:
        raise RuntimeError(f"manifest request failed: HTTP {response.status_code}")

    lines = [line.strip() for line in (response.text or "").splitlines() if line.strip()]
    segment_urls = [urljoin(candidate.url, line) for line in lines if not line.startswith("#")]
    if not segment_urls:
        raise RuntimeError("manifest contains no segments")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    ts_path = out_file.with_suffix(".ts")
    headers = _request_headers(capture, candidate.referer or candidate.url)

    with ts_path.open("wb") as ts:
        for idx, seg_url in enumerate(segment_urls, start=1):
            seg_resp = requests.get(seg_url, headers=headers, timeout=30)
            if not seg_resp.ok:
                raise RuntimeError(f"segment request failed at {idx}/{len(segment_urls)}: HTTP {seg_resp.status_code}")

            payload = _extract_png_tail_payload(seg_resp.content)
            if idx == 1 and not _is_mpeg_ts(payload):
                raise RuntimeError("segment payload is not MPEG-TS after PNG extraction")
            ts.write(payload)
            if idx % 50 == 0 or idx == len(segment_urls):
                print(f"[i] Repacked segments: {idx}/{len(segment_urls)}")

    remux_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "info",
        "-i",
        str(ts_path),
        "-c",
        "copy",
        str(out_file),
    ]
    returncode = _run_command(remux_cmd)
    if returncode != 0:
        raise RuntimeError(f"ffmpeg remux failed with exit code {returncode}")

    try:
        ts_path.unlink(missing_ok=True)
    except Exception:
        pass

    return out_file
