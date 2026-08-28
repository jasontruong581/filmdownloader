"""Candidate selection and download orchestration.

This used to live inside the CLI, with `print()` calls wired into the control
flow, which made it unreachable from anything that is not a terminal. Every
message is now an event; the CLI subscribes with a console printer that
reproduces its previous output, and the job layer subscribes with one that
persists and streams.

Nothing here writes to stdout. That is the property that lets a server call it.
"""

from __future__ import annotations

import json
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from .capture import capture_page
from .detect import (
    build_request_headers,
    detect_candidates,
    extract_embed_urls,
    filter_candidates_by_host,
    precheck_hls_candidates,
)
from .download import download_with_ffmpeg
from .events import (
    CANDIDATE_ATTEMPT,
    CANDIDATE_REJECTED,
    CANDIDATES_FOUND,
    DOWNLOAD_COMPLETED,
    FAILED,
    INFO,
    STAGE_STARTED,
    EventSink,
    PipelineEvent,
)
from .preflight import TEXT_OUTPUT
from .models import CaptureResult, StreamCandidate
from .options import PipelineOptions

#: How many embed URLs a deep scan will follow.
MAX_EMBED_SCANS = 3

#: Reordering hook the CLI supplies for interactive picking. The pipeline itself
#: is non-interactive by construction; it never reads stdin.
Reorder = Callable[[list[StreamCandidate]], list[StreamCandidate]]


def _emit(sink: EventSink | None, event_kind: str, /, **payload) -> None:
    """Emit an event.

    The first two parameters are positional-only so a payload key can never
    collide with them; "kind" is a legitimate payload field for a candidate.
    """
    if sink is not None:
        sink(PipelineEvent(event_kind, payload))


# --- Candidate collection ----------------------------------------------------


def clone_candidate(candidate: StreamCandidate, source: str) -> StreamCandidate:
    return StreamCandidate(
        url=candidate.url,
        kind=candidate.kind,
        score=candidate.score,
        source=source,
        status_code=candidate.status_code,
        content_type=candidate.content_type,
        host=candidate.host,
        probe_duration=candidate.probe_duration,
        probe_bitrate=candidate.probe_bitrate,
        validation_note=candidate.validation_note,
        referer=candidate.referer,
    )


def merge_candidates(
    merged: "OrderedDict[str, StreamCandidate]",
    incoming: list[StreamCandidate],
    source: str,
) -> None:
    """Fold one stage's candidates into the running map.

    A higher score promotes the kind and referer with it, because those describe
    the better-scoring observation. Diagnostic fields are promoted regardless,
    since a later probe knows more than an earlier guess.
    """
    for candidate in incoming:
        new_item = clone_candidate(candidate, source)
        existing = merged.get(new_item.url)
        if existing is None:
            merged[new_item.url] = new_item
            continue

        if source not in existing.source.split(","):
            existing.source = f"{existing.source},{source}"

        if new_item.score > existing.score:
            existing.score = new_item.score
            existing.kind = new_item.kind
            existing.referer = new_item.referer

        if new_item.status_code is not None:
            existing.status_code = new_item.status_code
        if new_item.content_type:
            existing.content_type = new_item.content_type
        if new_item.validation_note:
            existing.validation_note = new_item.validation_note


def collect_candidates(
    base_capture: CaptureResult,
    options: PipelineOptions,
    on_event: EventSink | None = None,
) -> tuple[list[StreamCandidate], dict[str, int]]:
    merged: OrderedDict[str, StreamCandidate] = OrderedDict()
    stage_counts: dict[str, int] = {}

    main_candidates = detect_candidates(
        capture=base_capture, probe=options.probe, host_bonuses=options.host_bonuses
    )
    merge_candidates(merged, main_candidates, "main")
    stage_counts["main"] = len(main_candidates)

    if main_candidates:
        return sorted(merged.values(), key=lambda x: x.score, reverse=True), stage_counts

    embed_urls = extract_embed_urls(base_capture)
    stage_counts["embed_urls"] = len(embed_urls)

    if embed_urls:
        _emit(on_event, INFO, message=f"No direct stream detected. Deep scan {len(embed_urls)} embed URL(s).")

    for embed_idx, embed_url in enumerate(embed_urls[:MAX_EMBED_SCANS], start=1):
        _emit(on_event, INFO, message=f"Analyze embed: {embed_url}")
        waits = [max(options.wait, 12)]
        if options.extra_wait > 0:
            waits.append(max(options.wait, 12) + options.extra_wait)

        for phase, phase_wait in enumerate(waits, start=1):
            stage = f"embed{embed_idx}_phase{phase}"
            if len(waits) > 1:
                _emit(
                    on_event,
                    STAGE_STARTED,
                    stage=stage,
                    message=f"Embed phase {phase}/{len(waits)} wait={phase_wait}s",
                )

            embed_capture = capture_page(
                url=embed_url,
                wait_seconds=phase_wait,
                headless=not options.headed,
                try_play=True,
            )
            embed_candidates = detect_candidates(
                capture=embed_capture, probe=options.probe, host_bonuses=options.host_bonuses
            )
            merge_candidates(merged, embed_candidates, stage)
            stage_counts[stage] = len(embed_candidates)

    return sorted(merged.values(), key=lambda x: x.score, reverse=True), stage_counts


# --- Ranking -----------------------------------------------------------------


def probe_duration_seconds(path: Path) -> float | None:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, check=True, **TEXT_OUTPUT)
        data = json.loads(result.stdout or "{}")
        duration = float(data.get("format", {}).get("duration", 0))
        return duration if duration > 0 else None
    except Exception:
        return None


def _headers_block_for_ffprobe(capture: CaptureResult, candidate: StreamCandidate) -> str:
    headers = build_request_headers(capture, candidate.referer)
    headers.pop("User-Agent", None)
    if not headers:
        return ""
    return "\r\n".join(f"{k}: {v}" for k, v in headers.items()) + "\r\n"


def probe_candidate_media(capture: CaptureResult, candidate: StreamCandidate) -> None:
    """Score a candidate by what ffprobe can read from it.

    A very short stream is usually an advert or a preview, so it is penalized;
    length and bitrate are rewarded up to a cap.
    """
    cmd = ["ffprobe", "-v", "error"]

    if capture.user_agent:
        cmd.extend(["-user_agent", capture.user_agent])

    header_block = _headers_block_for_ffprobe(capture, candidate)
    if header_block:
        cmd.extend(["-headers", header_block])

    cmd.extend(["-show_entries", "format=duration,bit_rate", "-of", "json", candidate.url])

    try:
        result = subprocess.run(cmd, capture_output=True, check=True, **TEXT_OUTPUT)
        data = json.loads(result.stdout or "{}")
        fmt = data.get("format", {})
        duration = float(fmt.get("duration", 0) or 0)
        bitrate = int(float(fmt.get("bit_rate", 0) or 0))

        if duration > 0:
            candidate.probe_duration = duration
            if duration < 90:
                candidate.score -= 45
            elif duration < 180:
                candidate.score -= 20
            candidate.score += min(int(duration / 60), 80)
        if bitrate > 0:
            candidate.probe_bitrate = bitrate
            candidate.score += min(int(bitrate / 500_000), 25)
    except Exception:
        return


def rank_candidates_with_ffprobe(
    capture: CaptureResult,
    candidates: list[StreamCandidate],
    top_n: int,
) -> list[StreamCandidate]:
    for candidate in candidates[: max(top_n, 0)]:
        probe_candidate_media(capture, candidate)
    return sorted(candidates, key=lambda x: x.score, reverse=True)


def boost_preferred_hosts(candidates: list[StreamCandidate], prefer_hosts: list[str]) -> list[StreamCandidate]:
    if not prefer_hosts:
        return candidates

    normalized = [h.lower().strip() for h in prefer_hosts if h.strip()]
    if not normalized:
        return candidates

    for candidate in candidates:
        host = (candidate.host or "").lower()
        if any(host == h or host.endswith(f".{h}") for h in normalized):
            candidate.score += 35

    return sorted(candidates, key=lambda x: x.score, reverse=True)


def prepare_candidates(
    capture: CaptureResult,
    options: PipelineOptions,
    on_event: EventSink | None = None,
) -> tuple[list[StreamCandidate], dict[str, int], list[StreamCandidate]]:
    all_candidates, stage_counts = collect_candidates(capture, options, on_event)

    selected = list(all_candidates)

    if options.allow_hosts:
        before = len(selected)
        selected = filter_candidates_by_host(selected, options.allow_hosts)
        _emit(on_event, INFO, message=f"allow-host filter: {before} -> {len(selected)}")

    if options.prefer_hosts and selected:
        selected = boost_preferred_hosts(selected, options.prefer_hosts)
        _emit(on_event, INFO, message=f"prefer-host boost applied: {', '.join(options.prefer_hosts)}")

    if options.precheck_hls and selected:
        selected = precheck_hls_candidates(selected, capture)

    if options.rank_with_ffprobe and selected:
        selected = rank_candidates_with_ffprobe(capture, selected, options.rank_top_n)

    _emit(on_event, CANDIDATES_FOUND, count=len(selected), stage_counts=stage_counts)
    return selected, stage_counts, all_candidates


# --- Download ----------------------------------------------------------------


def resolve_download_capture(
    base_capture: CaptureResult,
    candidates: list[StreamCandidate],
    options: PipelineOptions,
    on_event: EventSink | None = None,
) -> CaptureResult:
    """Re-capture the embed page when the winning candidate came from one.

    The embed's own session, not the outer page's, is what its CDN authorizes.
    """
    if not candidates:
        return base_capture

    top = candidates[0]
    if "embed" not in (top.source or ""):
        return base_capture

    embed_urls = extract_embed_urls(base_capture)
    if not embed_urls:
        return base_capture

    embed_url = embed_urls[0]
    _emit(on_event, INFO, message=f"Refresh embed capture context for download: {embed_url}")
    try:
        return capture_page(
            url=embed_url,
            wait_seconds=max(options.wait, 15),
            headless=not options.headed,
            try_play=True,
        )
    except Exception as exc:  # noqa: BLE001
        _emit(on_event, INFO, message=f"Embed recapture failed, fallback to base capture: {exc}")
        return base_capture


def download_with_fallback(
    capture: CaptureResult,
    candidates: list[StreamCandidate],
    options: PipelineOptions,
    metadata_capture: CaptureResult | None = None,
    on_event: EventSink | None = None,
    reorder: Reorder | None = None,
) -> int:
    """Try candidates in order until one produces an acceptable file."""
    pick = options.pick
    start = pick - 1 if 0 < pick <= len(candidates) else 0
    ordered = candidates[start:] + candidates[:start]

    if reorder is not None:
        ordered = reorder(ordered)

    attempts = min(options.max_attempts, len(ordered))

    last_error: Exception | None = None
    for idx, selected in enumerate(ordered[:attempts], start=1):
        _emit(
            on_event,
            CANDIDATE_ATTEMPT,
            index=idx,
            total=attempts,
            kind=selected.kind,
            url=selected.url,
        )
        try:
            out_file = download_with_ffmpeg(
                capture=capture,
                candidate=selected,
                output_dir=options.output_dir,
                metadata_capture=metadata_capture,
            )
            duration = probe_duration_seconds(out_file)
            if duration is not None and duration < float(options.min_duration):
                _emit(
                    on_event,
                    CANDIDATE_REJECTED,
                    url=selected.url,
                    reason="too_short",
                    duration=duration,
                    minimum=options.min_duration,
                )
                try:
                    out_file.unlink(missing_ok=True)
                except Exception:
                    pass
                continue
            _emit(on_event, DOWNLOAD_COMPLETED, path=str(out_file), url=selected.url)
            return 0
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            _emit(on_event, CANDIDATE_REJECTED, url=selected.url, reason="error", error=str(exc))

    _emit(on_event, FAILED, reason="all_candidates_failed", error=str(last_error) if last_error else None)
    return 3


def run(
    capture: CaptureResult,
    options: PipelineOptions,
    on_event: EventSink | None = None,
    reorder: Reorder | None = None,
) -> tuple[int, list[StreamCandidate], dict[str, int], list[StreamCandidate]]:
    """Full selection and download for one capture.

    Returns the exit code plus the candidate sets, so a caller can report or
    persist them without re-running anything.
    """
    selected, stage_counts, all_candidates = prepare_candidates(capture, options, on_event)

    if not selected:
        _emit(on_event, FAILED, reason="no_candidates")
        return 2, selected, stage_counts, all_candidates

    download_capture = resolve_download_capture(capture, selected, options, on_event)
    code = download_with_fallback(
        download_capture,
        selected,
        options,
        metadata_capture=capture,
        on_event=on_event,
        reorder=reorder,
    )
    return code, selected, stage_counts, all_candidates
