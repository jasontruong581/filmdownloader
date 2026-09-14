"""Running FFmpeg without trusting it to exit.

Both download paths need this and neither can own it: `ffmpeg_executor` already
imports `download`, so anything the two share has to sit below both.

FFmpeg is given reconnect flags precisely so it survives a connection the far
end drops, and the cost of that is a process which never gives up on its own.
Asked to reconnect, it retries and reports indefinitely: the log moves, the
process is busy, and no bytes arrive. Something outside FFmpeg has to decide
that the transfer is over, and the only evidence that separates a transfer
which is working from one which is merely busy is the size of the file being
written.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

#: How long the output file may fail to grow before the attempt is abandoned.
#:
#: Generous, because opening a playlist and probing it legitimately writes
#: nothing for a while. Measured on bytes rather than on FFmpeg reporting: a
#: reconnecting FFmpeg reports forever, so chatter proves the process is alive
#: and says nothing about whether the transfer is.
STALL_TIMEOUT_SECONDS = 120.0

#: How often the output file is stat()ed. FFmpeg reports several times a
#: second, and a stat() per report would be many calls to answer a question
#: about a two-minute window.
SAMPLE_INTERVAL_SECONDS = 0.25

#: Seconds to wait after terminate() before killing.
TERMINATE_GRACE_SECONDS = 5.0


def output_size(path: Path) -> int:
    """Bytes on disk, treating an absent or unreadable file as none."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


class OutputWatchdog:
    """Whether the transfer has stopped advancing, measured on the output file.

    Liveness used to be measured on FFmpeg's own chatter, which is not the same
    question. A transfer was watched sitting at the same byte count for nineteen
    minutes while it announced "downloading" the whole time.

    The timeout and the sampling interval are arguments rather than globals
    because the two callers poll on different schedules: the job executor is
    already waking to check a cancel flag and samples on that beat, while the
    CLI has nothing else to wake for.
    """

    def __init__(
        self,
        path: Path,
        timeout: float = STALL_TIMEOUT_SECONDS,
        sample_interval: float = SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        self._path = path
        self._timeout = timeout
        self._sample_interval = sample_interval
        self._size = output_size(path)
        self._advanced_at = time.monotonic()
        #: None means "not yet sampled", which must always sample. A zero
        #: compared against a monotonic clock reads as a very old sample on a
        #: long-running machine and a very recent one just after boot.
        self._sampled_at: float | None = None

    def stalled(self) -> bool:
        now = time.monotonic()
        if self._sampled_at is not None and now - self._sampled_at < self._sample_interval:
            return False
        self._sampled_at = now

        size = output_size(self._path)
        if size > self._size:
            self._size = size
            self._advanced_at = now
            return False
        return now - self._advanced_at >= self._timeout

    def note(self) -> str:
        """What to add to FFmpeg's own output, which says nothing about this.

        `stalled` is a fault hint, so this line survives into the summary even
        when a reconnecting FFmpeg also filled the log with errors of its own.
        """
        return (
            f"stalled: no data written for {int(self._timeout)}s, "
            "so the transfer was abandoned"
        )


def stop_process(process: subprocess.Popen) -> None:
    """End FFmpeg, politely first so it can close the file it is writing."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
