"""The download executor contract.

Two executors exist on purpose. FFmpeg downloads what the browser or a site
plugin found, keeping the obfuscated-HLS repack, the per-site asset conversion,
and the tuned header and cookie injection. yt-dlp downloads what yt-dlp
resolved, keeping its own format merging and retry logic. Re-implementing either
inside the other would lose real capability.

The price of the split is exactly one thing: both must report through the same
event vocabulary. That is `videotrack.core.events`, and it is why a consumer can
stay engine-agnostic.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .events import EventSink, PipelineEvent, null_sink
from .models import CaptureResult, StreamCandidate


@dataclass
class DownloadRequest:
    """Everything an executor needs to fetch one item to one path."""

    out_file: Path
    #: Set for the FFmpeg path: a detected candidate plus its request context.
    capture: CaptureResult | None = None
    candidate: StreamCandidate | None = None
    #: Set for the yt-dlp path.
    page_url: str = ""
    format_id: str | None = None
    #: Resolved location of the ffmpeg binary, when it is not on PATH.
    ffmpeg_location: str | None = None
    extra: dict = field(default_factory=dict)


class DownloadCancelled(RuntimeError):
    """Raised by an executor when its cancel event was set."""


class DownloadExecutor(Protocol):
    name: str

    def run(
        self,
        request: DownloadRequest,
        cancel: threading.Event,
        on_event: EventSink,
    ) -> Path:
        """Download to `request.out_file` and return the final path.

        Must poll `cancel` and raise `DownloadCancelled` promptly when it is set,
        leaving no partial file behind. Must report progress through `on_event`
        using the shared vocabulary.
        """


def no_cancel() -> threading.Event:
    """An event that is never set, for callers that do not support cancelling."""
    return threading.Event()


def emit(on_event: EventSink | None, event: PipelineEvent) -> None:
    (on_event or null_sink)(event)
