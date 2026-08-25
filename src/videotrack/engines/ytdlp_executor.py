"""The yt-dlp download executor.

Downloads what yt-dlp resolved, so its own format merging, fragment retry, and
postprocessing are used rather than reimplemented.

Its progress hooks are mapped onto the shared vocabulary, including the phase
that distinguishes a split-format download's video pass from its audio pass. Raw
hook output would show progress reach 100 percent, reset, reach it again, and
then stall during the merge.

Cancellation is checked inside the hook, which is the only place yt-dlp yields
control during a download.
"""

from __future__ import annotations

import threading
from pathlib import Path

from ..core.events import (
    DOWNLOAD_COMPLETED,
    PHASE_DOWNLOADING,
    PHASE_DOWNLOADING_AUDIO,
    PHASE_DOWNLOADING_VIDEO,
    PHASE_MERGING,
    PHASE_POSTPROCESSING,
    EventSink,
    MonotonicProgress,
    PipelineEvent,
    Progress,
    progress_event,
)
from ..core.executor import DownloadCancelled, DownloadRequest
from .ytdlp_resolver import YtDlpOptions, _ytdlp_module


class _Cancelled(Exception):
    """Internal signal raised out of a progress hook."""


def _phase_for(info: dict) -> str:
    """Which pass of a possibly multi-file download this hook belongs to.

    A split-format pick downloads video-only, then audio-only, then merges.
    """
    vcodec = (info.get("vcodec") or "").lower()
    acodec = (info.get("acodec") or "").lower()
    has_video = vcodec not in {"", "none"}
    has_audio = acodec not in {"", "none"}
    if has_video and not has_audio:
        return PHASE_DOWNLOADING_VIDEO
    if has_audio and not has_video:
        return PHASE_DOWNLOADING_AUDIO
    return PHASE_DOWNLOADING


def _progress_from_hook(status: dict) -> Progress:
    downloaded = status.get("downloaded_bytes")
    total = status.get("total_bytes") or status.get("total_bytes_estimate")

    percent: float | None = None
    if isinstance(downloaded, (int, float)) and isinstance(total, (int, float)) and total > 0:
        percent = min(100.0, max(0.0, downloaded / total * 100.0))

    info = status.get("info_dict") or {}
    return Progress(
        phase=_phase_for(info if isinstance(info, dict) else {}),
        percent=percent,
        downloaded_bytes=int(downloaded) if isinstance(downloaded, (int, float)) else None,
        # Only a real total claims to be one; an estimate is still the best we
        # have, so it is reported, but a missing one stays missing.
        total_bytes=int(total) if isinstance(total, (int, float)) else None,
        speed_bps=float(status["speed"]) if isinstance(status.get("speed"), (int, float)) else None,
        eta_seconds=float(status["eta"]) if isinstance(status.get("eta"), (int, float)) else None,
    )


class YtDlpExecutor:
    name = "ytdlp"

    def __init__(self, options: YtDlpOptions | None = None) -> None:
        self.options = options or YtDlpOptions()

    def _params(self, request: DownloadRequest) -> dict:
        params: dict = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "socket_timeout": self.options.socket_timeout,
            "outtmpl": str(request.out_file),
            # FFmpeg stays the muxer: it is what merges split formats and remuxes
            # HLS, which is why it remains a hard requirement on both paths.
            "merge_output_format": request.out_file.suffix.lstrip(".") or "mp4",
        }
        if request.format_id:
            params["format"] = request.format_id
        if request.ffmpeg_location:
            params["ffmpeg_location"] = request.ffmpeg_location
        if self.options.cookies_from_browser:
            params["cookiesfrombrowser"] = (self.options.cookies_from_browser,)
        return params

    def run(
        self,
        request: DownloadRequest,
        cancel: threading.Event,
        on_event: EventSink,
    ) -> Path:
        module = _ytdlp_module()
        if module is None:
            raise RuntimeError("yt-dlp is not installed")

        folder = MonotonicProgress()

        def progress_hook(status: dict) -> None:
            if cancel.is_set():
                raise _Cancelled()
            state = status.get("status")
            if state == "downloading":
                on_event(progress_event(folder.fold(_progress_from_hook(status))))
            elif state == "finished":
                phase = _phase_for(status.get("info_dict") or {})
                on_event(progress_event(folder.fold(Progress(phase=phase, percent=100.0))))

        def postprocessor_hook(status: dict) -> None:
            if cancel.is_set():
                raise _Cancelled()
            if status.get("status") != "started":
                return
            name = (status.get("postprocessor") or "").lower()
            phase = PHASE_MERGING if "merger" in name else PHASE_POSTPROCESSING
            on_event(progress_event(folder.fold(Progress(phase=phase, percent=0.0))))

        params = self._params(request)
        params["progress_hooks"] = [progress_hook]
        params["postprocessor_hooks"] = [postprocessor_hook]

        request.out_file.parent.mkdir(parents=True, exist_ok=True)

        try:
            with module.YoutubeDL(params) as ydl:
                ydl.download([request.page_url])
        except _Cancelled:
            _discard_partials(request.out_file)
            raise DownloadCancelled("cancelled before completion") from None
        except Exception as exc:  # noqa: BLE001
            if cancel.is_set():
                _discard_partials(request.out_file)
                raise DownloadCancelled("cancelled before completion") from None
            _discard_partials(request.out_file)
            raise RuntimeError(f"yt-dlp failed: {exc}") from exc

        if not request.out_file.exists() or request.out_file.stat().st_size == 0:
            _discard_partials(request.out_file)
            raise RuntimeError("yt-dlp produced no output")

        on_event(PipelineEvent(DOWNLOAD_COMPLETED, {"path": str(request.out_file)}))
        return request.out_file


def _discard_partials(out_file: Path) -> None:
    """Remove yt-dlp's own leftovers alongside the target."""
    candidates = [out_file, *out_file.parent.glob(f"{out_file.name}*.part")]
    candidates.extend(out_file.parent.glob(f"{out_file.stem}.f*"))
    for path in candidates:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass
