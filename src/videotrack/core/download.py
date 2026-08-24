from __future__ import annotations

import html
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import requests

from .models import CaptureResult, StreamCandidate


@dataclass
class PageMetadata:
    video_code: str | None = None
    title: str | None = None
    actresses: list[str] | None = None
    description: str | None = None


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


def _quatvn_asset_suffix(url: str) -> str:
    path = urlparse(url).path or ""
    name = unquote(path.rsplit("/", 1)[-1])
    match = re.search(r"\((\d+)\)\.webp$", name, re.IGNORECASE)
    if match:
        return f"clip-{int(match.group(1)):02d}"
    stem = Path(name).stem
    return _safe_name(stem or "clip")


def _clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return _clean_text(match.group(1))


def _request_headers(capture: CaptureResult, referer: str) -> dict[str, str]:
    headers: dict[str, str] = {
        "User-Agent": capture.user_agent,
        "Referer": referer,
    }
    if capture.cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in capture.cookies.items())
    return headers


def fetch_page_metadata(capture: CaptureResult) -> PageMetadata:
    url = capture.final_url or capture.page_url
    headers = _request_headers(capture, url)

    try:
        response = requests.get(url, headers=headers, timeout=20)
    except requests.RequestException:
        return PageMetadata()

    if not response.ok:
        return PageMetadata()

    if response.apparent_encoding:
        response.encoding = response.apparent_encoding

    html_text = response.text or ""
    code = _first_match(
        html_text,
        r'<span[^>]*class=["\'][^"\']*video-code[^"\']*["\'][^>]*>(.*?)</span>',
    )
    title = _first_match(
        html_text,
        r'<h2[^>]*id=["\']page-title["\'][^>]*>(.*?)</h2>',
    ) or _first_match(
        html_text,
        r'<h2[^>]*class=["\'][^"\']*\bpage-title\b[^"\']*["\'][^>]*>(.*?)</h2>',
    ) or _first_match(
        html_text,
        r'<h2[^>]*class=["\'][^"\']*\bbreadcrumb\b[^"\']*["\'][^>]*>(.*?)</h2>',
    )
    actresses_match = re.search(
        r'<div[^>]*class=["\'][^"\']*actress-tag[^"\']*["\'][^>]*>(.*?)</div>',
        html_text,
        re.IGNORECASE | re.DOTALL,
    )
    description = _first_match(
        html_text,
        r'<div[^>]*class=["\'][^"\']*video-description[^"\']*["\'][^>]*>(.*?)</div>',
    )

    actresses: list[str] = []
    if actresses_match:
        raw_block = actresses_match.group(1)
        actresses = [
            _clean_text(name)
            for name in re.findall(r'title=["\']([^"\']+)["\']', raw_block, re.IGNORECASE)
            if _clean_text(name)
        ]
        if not actresses:
            actresses_plain = _clean_text(raw_block)
            actresses = [part.strip() for part in re.split(r"\s{2,}|,\s*", actresses_plain) if part.strip()]

    return PageMetadata(
        video_code=code or None,
        title=title or None,
        actresses=actresses or None,
        description=description or None,
    )


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


def _write_description_file(out_file: Path, metadata: PageMetadata) -> None:
    sidecar = out_file.with_name(f"{out_file.stem} description.txt")

    title = metadata.title or ""
    actresses = ", ".join(metadata.actresses or [])
    description = metadata.description or ""

    content = (
        f"title: {title}\n"
        f"acctress: {actresses}\n"
        f"Description: {description}\n"
    )
    sidecar.write_text(content, encoding="utf-8")


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


def _run_ffmpeg(cmd: list[str]) -> int:
    proc = subprocess.run(cmd, capture_output=False, text=True)
    return proc.returncode


def _download_to_temp_file(capture: CaptureResult, candidate: StreamCandidate, suffix: str) -> Path:
    headers = _request_headers(capture, capture.final_url or capture.page_url)
    response = requests.get(candidate.url, headers=headers, timeout=60, stream=True)
    if not response.ok:
        raise RuntimeError(f"asset request failed: HTTP {response.status_code}")

    temp_path = Path("logs") / f"quatvn_asset_{abs(hash(candidate.url))}{suffix}"
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    with temp_path.open("wb") as handle:
        for chunk in response.iter_content(chunk_size=1024 * 512):
            if chunk:
                handle.write(chunk)
    response.close()
    return temp_path


def _download_quatvn_stream_asset(
    capture: CaptureResult,
    candidate: StreamCandidate,
    out_file: Path,
) -> Path:
    temp_in = _download_to_temp_file(capture, candidate, ".webp")
    out_file.parent.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory(prefix="quatvn_frames_", dir="logs") as temp_dir_raw:
            temp_dir = Path(temp_dir_raw)
            frame_pattern = temp_dir / "frame_%05d.png"
            concat_path = temp_dir / "frames.txt"

            coalesce_cmd = [
                "magick",
                str(temp_in),
                "-coalesce",
                str(frame_pattern),
            ]
            if _run_ffmpeg(coalesce_cmd) != 0:
                raise RuntimeError("magick failed to extract quatvn webp frames")

            identify_cmd = [
                "magick",
                "identify",
                "-format",
                "%T\n",
                str(temp_in),
            ]
            identify = subprocess.run(identify_cmd, capture_output=True, text=True)
            if identify.returncode != 0:
                raise RuntimeError("magick failed to read quatvn webp frame delays")

            delays = []
            for line in (identify.stdout or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    delays.append(max(float(line) / 100.0, 0.04))
                except ValueError:
                    delays.append(0.04)

            frames = sorted(temp_dir.glob("frame_*.png"))
            if not frames:
                raise RuntimeError("no frames extracted from quatvn webp asset")

            lines: list[str] = []
            for idx, frame in enumerate(frames):
                lines.append(f"file '{frame.resolve().as_posix()}'")
                if idx < len(frames) - 1:
                    duration = delays[idx] if idx < len(delays) else 0.04
                    lines.append(f"duration {duration:.3f}")
            lines.append(f"file '{frames[-1].resolve().as_posix()}'")
            concat_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

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
            if _run_ffmpeg(encode_cmd) != 0:
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
    page_id = _extract_page_id(metadata_source.final_url or metadata_source.page_url)
    preferred_base = metadata.video_code or None
    if page_id and metadata.video_code:
        preferred_base = f"{page_id}-{metadata.video_code}"
    if candidate.kind == "quatvn_webp":
        base_root = preferred_base or _safe_name(metadata_source.title or "quatvn")
        preferred_base = f"{base_root}-{_quatvn_asset_suffix(candidate.url)}"
    out_file = output_file or output_path_for(capture, output_dir, preferred_base=preferred_base)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    if candidate.kind == "quatvn_webp":
        out_file = _download_quatvn_stream_asset(capture, candidate, out_file)
        if write_metadata:
            _write_description_file(out_file, metadata)
        return out_file

    cmd = build_ffmpeg_command(capture, candidate, out_file)

    if _run_ffmpeg(cmd) != 0:
        if candidate.kind in {"hls", "playlist"} or ".m3u8" in candidate.url.lower() or "manifest" in candidate.url.lower():
            out = download_obfuscated_hls(capture, candidate, out_file)
            if write_metadata:
                _write_description_file(out, metadata)
            return out
        raise RuntimeError("ffmpeg failed")

    if write_metadata:
        _write_description_file(out_file, metadata)
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
    remux = subprocess.run(remux_cmd, capture_output=False, text=True)
    if remux.returncode != 0:
        raise RuntimeError(f"ffmpeg remux failed with exit code {remux.returncode}")

    try:
        ts_path.unlink(missing_ok=True)
    except Exception:
        pass

    return out_file
