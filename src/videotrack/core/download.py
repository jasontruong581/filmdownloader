"""Download a selected candidate through FFmpeg.

Site-neutral. Metadata extraction, per-site filename quirks, and asset
conversion are asked of the site registry rather than implemented here, so a
universal download does not get named from selectors that only exist on one
site.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Callable, Iterable
from functools import lru_cache
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .executor import DownloadCancelled
from .models import CaptureResult, PageMetadata, StreamCandidate
from .preflight import TEXT_OUTPUT, resolve_tool

#: How much of FFmpeg stderr to hold on to while an attempt runs.
#:
#: Generous, because FFmpeg states the reason it is refusing a stream while it
#: parses, and only afterwards describes whatever it did manage to read. A short
#: window keeps the description and loses the reason.
FFMPEG_STDERR_KEEP_LINES = 200

#: How many lines of diagnosis to hand back. A failure worth reading is a
#: sentence or two; a wall of text is what made the last one unreadable.
FFMPEG_ERROR_REPORT_LINES = 4

#: Substrings that mark a line as a diagnosis rather than a description.
#: Matched case-insensitively against whole lines of FFmpeg output.
FAULT_HINTS: tuple[str, ...] = (
    "error",
    "invalid",
    "failed",
    "unable to",
    "not found",
    "no such",
    "denied",
    "forbidden",
    "unauthorized",
    "not rfc",
    "not in allowed",
    "unsupported",
    "could not",
    "cannot",
    "timed out",
    "refused",
    "server returned",
    "end of file",
    "protocol not",
    "conversion failed",
    "moov atom",
    "connection reset",
    #: Not FFmpeg's word. The executor appends it when it abandons a transfer
    #: that stopped delivering, and that note is the whole diagnosis in a case
    #: where FFmpeg by definition said nothing.
    "stalled",
)


def _is_fault_line(line: str) -> bool:
    lowered = line.lower()
    return any(hint in lowered for hint in FAULT_HINTS)


def summarize_ffmpeg_error(lines: Iterable[str], returncode: int | None = None) -> str:
    """The part of FFmpeg's output that says what went wrong.

    Reporting the trailing lines produced an accurate account of a successful
    read attached to a failure: a stream listing, a `copy` mapping, and no
    mention of the fault, because the sentence naming it had already scrolled
    past. This searches the whole retained window for the diagnosis instead of
    trusting position.

    When FFmpeg genuinely said nothing diagnostic, that is reported as the
    finding rather than papered over: the exit code is then the only evidence
    there is, and it was previously dropped altogether on the repack path.
    """
    kept = [line.strip() for line in lines if line.strip()]
    faults = [line for line in kept if _is_fault_line(line)]
    if faults:
        return "\n".join(faults[-FFMPEG_ERROR_REPORT_LINES:])

    code = f" (exit code {returncode})" if returncode is not None else ""
    if not kept:
        return f"ffmpeg produced no output at all{code}"
    tail = "\n".join(kept[-FFMPEG_ERROR_REPORT_LINES:])
    return f"ffmpeg reported no error text{code}; its last output was:\n{tail}"


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


def postprocess_candidate(
    capture: CaptureResult, candidate: StreamCandidate, out_file: Path
) -> Path | None:
    """Let a site plugin fetch this candidate itself, if one claims its kind.

    Some assets are not something FFmpeg can download at all - an animated WebP
    is the bundled example - so the plugin produces the file and the caller skips
    FFmpeg entirely. Public because both download paths need it.
    """
    plugin = _site_plugin_for_kind(candidate.kind)
    if plugin is None:
        return None
    return plugin.postprocess(capture, candidate, out_file)


def is_hls_candidate(candidate: StreamCandidate) -> bool:
    """Whether this candidate is a playlist rather than a single file.

    Judged by kind first and URL shape second, because a real playlist is not
    always named `.m3u8`. Shared so the command builder and the repack fallback
    cannot disagree about what they are looking at.
    """
    url = candidate.url.lower()
    return candidate.kind in {"hls", "playlist"} or ".m3u8" in url or "manifest" in url


@lru_cache(maxsize=8)
def hls_strictness_flags(ffmpeg_location: str | None = None) -> tuple[str, ...]:
    """Flags that relax the HLS demuxer, limited to what this FFmpeg accepts.

    FFmpeg 7.1 added `extension_picky`, on by default, which rejects a segment
    whose extension it does not recognize *and* a playlist whose MIME type is
    not RFC 8216 compliant - both common on real sites, and neither loosened by
    `allowed_extensions` alone.

    Probed rather than assumed: passing an option an older build does not have
    makes it exit before downloading anything.

    The location is a parameter because the probe has to ask the *same* binary
    the command will run. Resolving it from the environment alone meant an
    operator who configured FFmpeg only in settings got no flags, and every
    playlist quietly took a slower fallback path instead of downloading
    directly.
    """
    location = Path(ffmpeg_location).expanduser() if ffmpeg_location else None
    binary = resolve_tool("ffmpeg", location) or "ffmpeg"
    try:
        result = subprocess.run(
            [binary, "-hide_banner", "-h", "demuxer=hls"],
            capture_output=True,
            **TEXT_OUTPUT,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    help_text = (result.stdout or "") + (result.stderr or "")
    return ("-extension_picky", "0") if "extension_picky" in help_text else ()


#: How long to keep retrying a dropped connection before giving up. FFmpeg
#: defaults to 120, which spends two minutes on a stream that is already gone.
RECONNECT_DELAY_MAX_SECONDS = 30

#: Reconnect on transient server faults only. A 4xx is a verdict, not a hiccup:
#: an expired token answers 403 forever, and retrying it would rebuild the very
#: hang these flags exist to remove.
RECONNECT_HTTP_STATUS = "5xx"

#: Every option is an input option and has to precede `-i`.
_RESILIENCE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("reconnect", "1"),
    ("reconnect_streamed", "1"),
    ("reconnect_on_network_error", "1"),
    ("reconnect_delay_max", str(RECONNECT_DELAY_MAX_SECONDS)),
    ("reconnect_on_http_error", RECONNECT_HTTP_STATUS),
)


@lru_cache(maxsize=8)
def network_resilience_flags(ffmpeg_location: str | None = None) -> tuple[str, ...]:
    """Flags that make FFmpeg survive a connection the far end drops.

    Every reconnect option ships off by default, so an interrupted transfer left
    FFmpeg blocked on a socket the server had already closed - observed as a
    download that sat at the same byte count for hours with its sockets in
    CLOSE_WAIT, never erroring and never exiting. The HTTP protocol offers no
    read timeout to catch that, which leaves reconnection as the only way the
    process notices.

    Probed against the same binary the command will run, for the reason
    `hls_strictness_flags` documents: an option this build does not have makes
    it exit before downloading anything.
    """
    location = Path(ffmpeg_location).expanduser() if ffmpeg_location else None
    binary = resolve_tool("ffmpeg", location) or "ffmpeg"
    try:
        result = subprocess.run(
            [binary, "-hide_banner", "-h", "protocol=http"],
            capture_output=True,
            **TEXT_OUTPUT,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    help_text = (result.stdout or "") + (result.stderr or "")

    flags: list[str] = []
    for name, value in _RESILIENCE_OPTIONS:
        if f"-{name}" in help_text:
            flags.extend([f"-{name}", value])
    return tuple(flags)


def is_network_candidate(candidate: StreamCandidate) -> bool:
    """Whether this input is fetched over HTTP, and so can stall mid-transfer."""
    return candidate.url.lower().startswith(("http://", "https://"))


def fetch_page_metadata(capture: CaptureResult, page_html: str | None = None) -> PageMetadata:
    """Descriptive fields from whichever site plugin claims this page."""
    plugin = _site_plugin_for(capture.final_url or capture.page_url)
    if plugin is None:
        return PageMetadata()
    try:
        return plugin.metadata(capture, page_html) or PageMetadata()
    except Exception:  # noqa: BLE001 - naming must never fail a download
        return PageMetadata()


def path_key(path: Path | str) -> str:
    """Comparison key for a path: absolute, and case-folded where the OS is."""
    return os.path.normcase(os.path.abspath(str(path)))


def unique_path(path: Path, taken: Iterable[str] = ()) -> Path:
    """Return `path`, or the first free `name (n).ext` beside it.

    Two pages can easily derive the same name. FFmpeg runs with `-y`, so without
    this the second download silently overwrites the first.

    `taken` names paths that are reserved but not written yet. Existence alone is
    not enough for that case: a queued job has no file on disk, so checking the
    filesystem hands one name to every job that derives it.
    """
    reserved = {path_key(item) for item in taken}

    def free(candidate: Path) -> bool:
        return not candidate.exists() and path_key(candidate) not in reserved

    if free(path):
        return path
    for counter in range(2, 1000):
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if free(candidate):
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
    ffmpeg_location: str | None = None,
) -> list[str]:
    # Resolved here rather than by the caller. The job executor fixed up argv[0]
    # after building, so the server ran the configured binary while every other
    # caller - the CLI, and the pipeline it shares - ran a bare name that only
    # works when FFmpeg happens to be on PATH. Doing it once, where the command
    # is built, is what makes that impossible to get wrong again.
    location = Path(ffmpeg_location).expanduser() if ffmpeg_location else None
    cmd = [
        resolve_tool("ffmpeg", location) or "ffmpeg",
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

    if is_network_candidate(candidate):
        # Not restricted to playlists: a single file served over HTTP stalls the
        # same way, and has the same nothing to fall back on.
        cmd.extend(network_resilience_flags(ffmpeg_location))

    if is_hls_candidate(candidate):
        cmd.extend(
            [
                "-protocol_whitelist",
                "file,http,https,tcp,tls,crypto,data",
                "-allowed_extensions",
                "ALL",
            ]
        )
        cmd.extend(hls_strictness_flags(ffmpeg_location))

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


def _discard_partial(path: Path) -> None:
    """Remove a partial file, tolerating a Windows lock held by a dying process."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _run_command(cmd: list[str]) -> int:
    """Run an external tool, letting its output through to the terminal.

    A missing binary raises FileNotFoundError rather than returning non-zero, so
    it is translated into a message that names the tool.
    """
    try:
        return subprocess.run(cmd, capture_output=False, **TEXT_OUTPUT).returncode
    except FileNotFoundError as exc:
        raise ToolNotFound(f"{cmd[0]} not found. Install it or set its location in settings.") from exc


def _run_ffmpeg_captured(cmd: list[str]) -> tuple[int, str]:
    """Run FFmpeg, keeping the tail of stderr so a failure can be explained."""
    try:
        proc = subprocess.run(cmd, capture_output=True, **TEXT_OUTPUT)
    except FileNotFoundError as exc:
        raise ToolNotFound(f"{cmd[0]} not found. Install it or set its location in settings.") from exc

    stderr = proc.stderr or ""
    if stderr:
        print(stderr, end="" if stderr.endswith("\n") else "\n")
    summary = summarize_ffmpeg_error(stderr.splitlines(), proc.returncode)
    return proc.returncode, summary


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

    converted = postprocess_candidate(capture, candidate, out_file)
    if converted is not None:
        if write_metadata:
            _write_sidecar(metadata_source, converted, metadata)
        return converted

    cmd = build_ffmpeg_command(capture, candidate, out_file)
    returncode, stderr_tail = _run_ffmpeg_captured(cmd)

    if returncode != 0:
        if is_hls_candidate(candidate):
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


def download_obfuscated_hls(
    capture: CaptureResult,
    candidate: StreamCandidate,
    out_file: Path,
    cancel: threading.Event | None = None,
    on_progress: Callable[[int, int], None] | None = None,
    ffmpeg_location: str | None = None,
) -> Path:
    """Repack an HLS stream FFmpeg cannot read, segment by segment.

    Host-neutral: the technique is detected from the payload, not from a domain.
    Handles segments wrapped behind a PNG header and segments served with no
    usable extension, neither of which FFmpeg's own HLS demuxer will accept.

    `on_progress` receives (done, total) per segment. A caller that passes it
    gets no stdout at all, which is what lets a server use this; the CLI passes
    nothing and keeps printing. `cancel` is polled between segments, the only
    place this can stop without leaving a torn file.
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

    total = len(segment_urls)
    with ts_path.open("wb") as ts:
        for idx, seg_url in enumerate(segment_urls, start=1):
            if cancel is not None and cancel.is_set():
                ts.close()
                _discard_partial(ts_path)
                raise DownloadCancelled("cancelled while repacking segments")

            seg_resp = requests.get(seg_url, headers=headers, timeout=30)
            if not seg_resp.ok:
                raise RuntimeError(f"segment request failed at {idx}/{total}: HTTP {seg_resp.status_code}")

            payload = _extract_png_tail_payload(seg_resp.content)
            if idx == 1 and not _is_mpeg_ts(payload):
                raise RuntimeError("segment payload is not usable media after unwrapping")
            ts.write(payload)

            if on_progress is not None:
                on_progress(idx, total)
            elif idx % 50 == 0 or idx == total:
                print(f"[i] Repacked segments: {idx}/{total}")

    # Resolved rather than named: FFmpeg is regularly configured in Settings
    # only, never placed on PATH, and this step used to ask for a bare `ffmpeg`.
    # Every segment would download, and the repack then failed at the last
    # moment claiming FFmpeg was not installed - by the same FFmpeg that had
    # already run the attempt this fallback exists to rescue.
    location = Path(ffmpeg_location).expanduser() if ffmpeg_location else None
    remux_cmd = [
        resolve_tool("ffmpeg", location) or "ffmpeg",
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
