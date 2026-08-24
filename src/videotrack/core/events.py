"""The one progress vocabulary every engine reports in.

Two executors download: FFmpeg for candidates the browser or a site plugin
found, yt-dlp for what yt-dlp resolved. They emit different raw signals, so this
module fixes the shape both must normalize into. Defining it once is what makes
"the UI does not care which engine ran" true rather than aspirational.

Three facts the shape has to respect, learned from what the tools actually emit:

* **Percent can be unknown.** FFmpeg reports elapsed media time, which needs a
  probed duration that some HLS streams do not provide. yt-dlp sometimes has
  neither a total nor an estimate on fragmented streams. `percent` is therefore
  optional and a renderer must have an indeterminate state; faking zero would
  report a stalled download.
* **Totals mean different things.** FFmpeg's running total is bytes written so
  far, not the final size. Only `total_bytes` claims to be a size, and it is
  optional too.
* **One download can be several files.** A yt-dlp split-format pick fetches
  video, then audio, then merges. `phase` distinguishes them so a consumer can
  aggregate into one monotonic track instead of showing progress reach 100
  percent twice and then stall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

# --- Event kinds -------------------------------------------------------------

STAGE_STARTED = "stage_started"
CANDIDATES_FOUND = "candidates_found"
CANDIDATE_ATTEMPT = "candidate_attempt"
CANDIDATE_REJECTED = "candidate_rejected"
PROGRESS = "progress"
DOWNLOAD_COMPLETED = "download_completed"
FAILED = "failed"
INFO = "info"

EVENT_KINDS: tuple[str, ...] = (
    STAGE_STARTED,
    CANDIDATES_FOUND,
    CANDIDATE_ATTEMPT,
    CANDIDATE_REJECTED,
    PROGRESS,
    DOWNLOAD_COMPLETED,
    FAILED,
    INFO,
)

# --- Download phases ---------------------------------------------------------

PHASE_DOWNLOADING = "downloading"
PHASE_DOWNLOADING_VIDEO = "downloading:video"
PHASE_DOWNLOADING_AUDIO = "downloading:audio"
PHASE_MERGING = "merging"
PHASE_POSTPROCESSING = "postprocessing"

DOWNLOAD_PHASES: tuple[str, ...] = (
    PHASE_DOWNLOADING,
    PHASE_DOWNLOADING_VIDEO,
    PHASE_DOWNLOADING_AUDIO,
    PHASE_MERGING,
    PHASE_POSTPROCESSING,
)


@dataclass(frozen=True)
class Progress:
    """A normalized progress sample. Every measurement is optional."""

    phase: str = PHASE_DOWNLOADING
    percent: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed_bps: float | None = None
    eta_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "percent": self.percent,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed_bps": self.speed_bps,
            "eta_seconds": self.eta_seconds,
        }


@dataclass(frozen=True)
class PipelineEvent:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "payload": self.payload}


EventSink = Callable[[PipelineEvent], None]


def progress_event(progress: Progress) -> PipelineEvent:
    return PipelineEvent(PROGRESS, progress.to_dict())


def null_sink(event: PipelineEvent) -> None:
    """Discard events. Used when a caller wants no output at all."""


# --- Multi-file aggregation --------------------------------------------------


class MonotonicProgress:
    """Fold a multi-file download into one non-decreasing progress track.

    A split-format download reports its video stream 0-100, then its audio
    stream 0-100, then merges. Passing those through unchanged makes the bar
    jump backwards. This weights each known phase and never emits a percent
    lower than one already reported.
    """

    #: Fractions of the whole job each phase represents.
    PHASE_WEIGHTS: dict[str, tuple[float, float]] = {
        PHASE_DOWNLOADING: (0.0, 1.0),
        PHASE_DOWNLOADING_VIDEO: (0.0, 0.8),
        PHASE_DOWNLOADING_AUDIO: (0.8, 0.95),
        PHASE_MERGING: (0.95, 0.99),
        PHASE_POSTPROCESSING: (0.99, 1.0),
    }

    def __init__(self) -> None:
        self._highest: float | None = None

    def fold(self, progress: Progress) -> Progress:
        start, end = self.PHASE_WEIGHTS.get(progress.phase, (0.0, 1.0))

        if progress.percent is None:
            # Unknown within the phase: report the phase floor, still monotonic.
            scaled = start * 100.0 if self._highest is None else self._highest
            return Progress(
                phase=progress.phase,
                percent=None if self._highest is None and start == 0.0 else scaled,
                downloaded_bytes=progress.downloaded_bytes,
                total_bytes=progress.total_bytes,
                speed_bps=progress.speed_bps,
                eta_seconds=progress.eta_seconds,
            )

        fraction = max(0.0, min(progress.percent, 100.0)) / 100.0
        scaled = (start + (end - start) * fraction) * 100.0
        if self._highest is not None:
            scaled = max(scaled, self._highest)
        self._highest = scaled

        return Progress(
            phase=progress.phase,
            percent=round(scaled, 2),
            downloaded_bytes=progress.downloaded_bytes,
            total_bytes=progress.total_bytes,
            speed_bps=progress.speed_bps,
            eta_seconds=progress.eta_seconds,
        )
