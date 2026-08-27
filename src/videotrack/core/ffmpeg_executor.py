"""The FFmpeg download executor.

Downloads what the browser or a site plugin found. Reports progress by parsing
FFmpeg's own `-progress` stream, retains the tail of stderr so a failure can be
explained, and is cancellable.

Cancellation writes to a `.part` file and renames only on success. On Windows a
terminated FFmpeg can briefly hold its output open, so a failed unlink is
tolerated rather than fatal.

FFmpeg is never trusted to exit on its own. Its output is read on a thread so
this executor keeps control of its own waiting: a stream that stops delivering
data leaves FFmpeg blocked on a socket forever, and reading its pipe directly
meant that hang became the job's hang, with the cancel flag unreachable.
"""

from __future__ import annotations

import queue
import subprocess
import threading
import time
from pathlib import Path

from .download import (
    FFMPEG_STDERR_KEEP_LINES,
    ToolNotFound,
    build_ffmpeg_command,
    download_obfuscated_hls,
    is_hls_candidate,
    postprocess_candidate,
    summarize_ffmpeg_error,
)
from .events import (
    DOWNLOAD_COMPLETED,
    FAILED,
    INFO,
    PHASE_DOWNLOADING,
    EventSink,
    MonotonicProgress,
    PipelineEvent,
    Progress,
    progress_event,
)
from .executor import DownloadCancelled, DownloadRequest
from .ffmpeg_progress import FfmpegProgressParser
from .preflight import resolve_tool

#: Where the diagnosis is searched for, rather than where it is assumed to be.
#: Owned by `download` so the two places that read FFmpeg stderr agree.
STDERR_TAIL_LINES = FFMPEG_STDERR_KEEP_LINES

#: Seconds to wait after terminate() before killing.
TERMINATE_GRACE_SECONDS = 5.0

#: How often the reader loop checks the cancel flag while waiting on output.
CANCEL_POLL_SECONDS = 0.25

#: How long FFmpeg may produce nothing at all before the attempt is abandoned.
#:
#: Generous, because opening a playlist and probing it is legitimately quiet for
#: a while, and a slow-but-live transfer still reports as it muxes. Silence for
#: this long means the transfer is not slow, it is stopped.
STALL_TIMEOUT_SECONDS = 120.0

#: Pushed by the reader thread once FFmpeg closes stdout, so the loop can tell
#: "finished" from "still waiting" without polling the process.
_STDOUT_CLOSED = object()


def _with_progress_flags(cmd: list[str]) -> list[str]:
    """Insert the flags that make FFmpeg emit a machine-readable progress stream."""
    if not cmd:
        return cmd
    return [cmd[0], "-nostats", "-progress", "pipe:1", *cmd[1:]]


def _resolved_binary(cmd: list[str], ffmpeg_location: str | None) -> list[str]:
    """Point argv[0] at the configured FFmpeg.

    The setting names a directory or the executable itself, the same two forms
    `preflight.resolve_tool` accepts, because that is what the environment
    variable and the Settings field are documented to take. Substituting the
    value verbatim turned a directory into an unrunnable argv[0].

    An unusable location leaves the command alone so the existing missing-tool
    error still surfaces rather than being masked by a path that cannot run.
    """
    if not ffmpeg_location or not cmd:
        return cmd
    resolved = resolve_tool(cmd[0], Path(ffmpeg_location))
    return [resolved or cmd[0], *cmd[1:]]


class FfmpegExecutor:
    name = "ffmpeg"

    def __init__(self, duration_hint: float | None = None) -> None:
        #: Percent needs a duration. Some HLS streams never report one, in which
        #: case progress stays honest about being unknown.
        self.duration_hint = duration_hint

    def run(
        self,
        request: DownloadRequest,
        cancel: threading.Event,
        on_event: EventSink,
    ) -> Path:
        if request.capture is None or request.candidate is None:
            raise ValueError("the ffmpeg executor needs a capture and a candidate")

        # A plugin that claims this kind fetches the asset itself. An animated
        # WebP is the bundled example: FFmpeg cannot download it at all.
        converted = postprocess_candidate(request.capture, request.candidate, request.out_file)
        if converted is not None:
            on_event(progress_event(Progress(phase=PHASE_DOWNLOADING, percent=100.0)))
            on_event(PipelineEvent(DOWNLOAD_COMPLETED, {"path": str(converted)}))
            return converted

        part_file = request.out_file.with_name(f"{request.out_file.stem}.part{request.out_file.suffix}")
        part_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            part_file.unlink(missing_ok=True)
        except OSError:
            pass

        cmd = build_ffmpeg_command(
            request.capture, request.candidate, part_file, request.ffmpeg_location
        )
        cmd = _with_progress_flags(_resolved_binary(cmd, request.ffmpeg_location))

        parser = FfmpegProgressParser(duration_seconds=self.duration_hint, phase=PHASE_DOWNLOADING)
        folder = MonotonicProgress()

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise ToolNotFound(
                f"{cmd[0]} not found. Install it or set its location in settings."
            ) from exc

        stderr_tail: list[str] = []
        stderr_thread = threading.Thread(
            target=_drain_stderr, args=(process, stderr_tail), daemon=True
        )
        stderr_thread.start()

        lines: queue.Queue = queue.Queue()
        stdout_thread = threading.Thread(target=_drain_stdout, args=(process, lines), daemon=True)
        stdout_thread.start()

        cancelled = False
        stalled = False
        last_output = time.monotonic()
        try:
            while True:
                try:
                    line = lines.get(timeout=CANCEL_POLL_SECONDS)
                except queue.Empty:
                    # The only place cancellation and a stall can be noticed:
                    # FFmpeg says nothing in either case.
                    if cancel.is_set():
                        cancelled = True
                        break
                    if time.monotonic() - last_output >= STALL_TIMEOUT_SECONDS:
                        stalled = True
                        break
                    continue

                if line is _STDOUT_CLOSED:
                    break
                if cancel.is_set():
                    cancelled = True
                    break

                # Any line proves FFmpeg is alive, whether or not it parses.
                last_output = time.monotonic()
                sample = parser.feed(line)
                if sample is not None:
                    on_event(progress_event(folder.fold(sample)))
        finally:
            if cancelled or stalled or cancel.is_set():
                _stop(process)
            returncode = process.wait()
            stderr_thread.join(timeout=2.0)
            stdout_thread.join(timeout=2.0)

        if cancelled or cancel.is_set():
            _discard(part_file)
            raise DownloadCancelled("cancelled before completion")

        if stalled:
            # Said before FFmpeg's own tail, because FFmpeg reports nothing at
            # all about this: the last thing it printed was a normal line, and
            # the exit code only records that it was terminated.
            stderr_tail.append(
                f"stalled: no output for {int(STALL_TIMEOUT_SECONDS)}s, "
                "so the transfer was abandoned"
            )

        if returncode != 0:
            _discard(part_file)
            detail = summarize_ffmpeg_error(stderr_tail, returncode)
            if is_hls_candidate(request.candidate):
                # FFmpeg refuses plenty of real playlists: segments served with
                # no usable extension, a MIME type that is not RFC 8216
                # compliant, or a payload wrapped behind another format's
                # header. Fetching the segments directly is the only thing that
                # reads those, so a failure here is not the end of the attempt.
                return self._repack(request, cancel, on_event, detail, returncode)
            on_event(PipelineEvent(FAILED, {"reason": "ffmpeg_failed", "error": detail}))
            raise RuntimeError(
                f"ffmpeg failed with exit code {returncode}" + (f": {detail}" if detail else "")
            )

        if not part_file.exists() or part_file.stat().st_size == 0:
            _discard(part_file)
            raise RuntimeError("ffmpeg produced an empty output")

        part_file.replace(request.out_file)
        on_event(progress_event(folder.fold(Progress(phase=PHASE_DOWNLOADING, percent=100.0))))
        on_event(PipelineEvent(DOWNLOAD_COMPLETED, {"path": str(request.out_file)}))
        return request.out_file


    def _repack(
        self,
        request: DownloadRequest,
        cancel: threading.Event,
        on_event: EventSink,
        ffmpeg_detail: str,
        ffmpeg_returncode: int,
    ) -> Path:
        """Fetch the playlist segments directly when FFmpeg would not read them.

        Progress is reported through the callback rather than printed, which is
        what lets a server use this path, and the segment loop polls the cancel
        event so a long repack stays interruptible.
        """
        assert request.capture is not None and request.candidate is not None
        on_event(
            PipelineEvent(
                INFO,
                {"message": "FFmpeg refused this playlist; fetching its segments directly."},
            )
        )

        def report(done: int, total: int) -> None:
            percent = (done / total * 100.0) if total else None
            on_event(progress_event(Progress(phase=PHASE_DOWNLOADING, percent=percent)))

        try:
            out = download_obfuscated_hls(
                request.capture,
                request.candidate,
                request.out_file,
                cancel=cancel,
                on_progress=report,
                ffmpeg_location=request.ffmpeg_location,
            )
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            # The repack failing is what ends the attempt, so it leads. What
            # FFmpeg said is context, and the exit code belongs with it: it was
            # dropped here entirely, which left this path unable to say anything
            # at all about why the direct attempt was abandoned.
            error = (
                f"the repack failed: {exc}"
                f" (ffmpeg had already exited {ffmpeg_returncode}: "
                f"{ffmpeg_detail or 'no detail'})"
            )
            on_event(PipelineEvent(FAILED, {"reason": "repack_failed", "error": error}))
            raise RuntimeError(error) from exc

        on_event(progress_event(Progress(phase=PHASE_DOWNLOADING, percent=100.0)))
        on_event(PipelineEvent(DOWNLOAD_COMPLETED, {"path": str(out)}))
        return out


def _drain_stdout(process: subprocess.Popen, lines: queue.Queue) -> None:
    """Move FFmpeg's progress stream onto a queue the caller can poll.

    The sentinel is pushed from `finally` so a reader that dies still releases
    the loop, rather than leaving it to time out as a stall it is not.
    """
    try:
        if process.stdout is not None:
            for line in process.stdout:
                lines.put(line)
    finally:
        lines.put(_STDOUT_CLOSED)


def _drain_stderr(process: subprocess.Popen, tail: list[str]) -> None:
    if process.stderr is None:
        return
    for line in process.stderr:
        tail.append(line.rstrip())
        if len(tail) > STDERR_TAIL_LINES * 4:
            del tail[: len(tail) - STDERR_TAIL_LINES]


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()


def _discard(path: Path) -> None:
    """Remove a partial file, tolerating a Windows lock held by a dying process."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
