"""The worker pool.

Threads rather than asyncio: the work is blocking subprocess and requests code,
and a server bridges with `run_in_threadpool`.

Concurrency is gated by an admission count rather than the pool size, because a
`ThreadPoolExecutor` cannot shrink and the setting has to apply without a
restart. Changing the limit never waits for a running job: it only changes what
the next worker is admitted against.

Cancellation honesty, stated once here because it reaches the UI: a browser
capture has no interruption hook. Cancel takes effect between pipeline stages and
before each candidate attempt, so an in-flight Chrome capture finishes first. The
UI says "cancelling" rather than pretending it is immediate.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from ..core.download import output_path_for, unique_path
from ..core.events import (
    CANDIDATE_ATTEMPT,
    CANDIDATE_REJECTED,
    DOWNLOAD_COMPLETED,
    FAILED,
    PROGRESS,
    PipelineEvent,
)
from ..core.executor import DownloadCancelled, DownloadRequest
from ..core.options import PipelineOptions
from ..core.resolvers import Resolution, auth_wall_reason, capture_from_resolution
from .bus import EventBus
from .cache import ResolutionCache
from .models import Batch, Job, JobStatus
from .store import JobStore

DEFAULT_CONCURRENCY = 2
MAX_POOL_WORKERS = 16

#: Shortest gap between two progress lines in the log, per job. FFmpeg reports
#: several times a second; unthrottled, those lines bury the stage messages that
#: say what the work is actually doing.
PROGRESS_LOG_INTERVAL_SECONDS = 5.0

logger = logging.getLogger(__name__)

#: Injected so tests can drive the manager without FFmpeg, yt-dlp, or Chrome.
Runner = Callable[[Job, Resolution | None, threading.Event, Callable[[PipelineEvent], None]], Path]


class DuplicateJob(RuntimeError):
    """A job for the same URL and format is already active."""


@dataclass
class JobManager:
    store: JobStore
    bus: EventBus
    cache: ResolutionCache | None = None
    options: PipelineOptions | None = None
    concurrency: int = DEFAULT_CONCURRENCY
    runner: Runner | None = None

    def __post_init__(self) -> None:
        self.cache = self.cache or ResolutionCache()
        self.options = self.options or PipelineOptions()
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        #: Admission gate. Shares the manager lock so a slot change and a
        #: cancellation cannot observe half-updated state.
        self._gate = threading.Condition(self._lock)
        self._running = 0
        #: Serializes claim-then-record. Kept apart from the manager lock so a
        #: submit never contends with admission or cancellation.
        self._claim_lock = threading.Lock()
        #: Per job, when a progress line was last written to the log.
        self._last_progress_log: dict[str, float] = {}
        self._pool = ThreadPoolExecutor(max_workers=MAX_POOL_WORKERS, thread_name_prefix="job")
        self._closed = False
        self.set_concurrency(self.concurrency)

    # --- concurrency ---------------------------------------------------------

    def set_concurrency(self, value: int) -> None:
        """Change how many jobs run at once, effective immediately.

        Never waits for a job. Lowering the limit lets whatever is in flight
        finish and simply stops admitting replacements; raising it wakes the
        waiters. Waiting here previously deadlocked outright: this held the
        manager lock while a worker needed that same lock to release its slot.
        """
        target = max(1, min(int(value), MAX_POOL_WORKERS))
        with self._gate:
            self.concurrency = target
            self._gate.notify_all()

    def _admit(self) -> bool:
        """Claim a slot, waiting for one. False if the manager shut down first."""
        with self._gate:
            while not self._closed and self._running >= self.concurrency:
                self._gate.wait()
            if self._closed:
                return False
            self._running += 1
            return True

    def _release_slot(self) -> None:
        with self._gate:
            self._running -= 1
            self._gate.notify_all()

    # --- submission ----------------------------------------------------------

    def submit(
        self,
        url: str,
        *,
        resolution_id: str | None = None,
        format_id: str | None = None,
        title: str = "",
        engine: str = "",
        batch_id: str | None = None,
        output_dir: Path | None = None,
    ) -> Job:
        existing = self.store.active_for(url, format_id)
        if existing is not None:
            raise DuplicateJob(f"job {existing.id} for this URL and format is already {existing.status.value}")

        job = Job(
            url=url,
            resolution_id=resolution_id,
            format_id=format_id,
            title=title,
            engine=engine,
            batch_id=batch_id,
        )
        # Claiming and recording are one step. Two concurrent submits would
        # otherwise both read the same free name before either was stored, and
        # under `ffmpeg -y` the two workers would write one file.
        with self._claim_lock:
            job.output_path = str(self._claim_output_path(job, output_dir))
            self.store.add(job)
        self._publish(job, PipelineEvent("job_queued", {"status": job.status.value}))
        self._pool.submit(self._run_job, job.id)
        return job

    def submit_batch(
        self,
        items,
        *,
        source_url: str = "",
        capability: str = "",
        confidence: str = "",
        output_dir: Path | None = None,
    ) -> tuple[Batch, list[Job], list[str]]:
        """Queue many items as independent jobs sharing a batch id.

        One item failing to queue never prevents the others: the batch is a
        grouping, not a transaction.
        """
        batch = self.store.add_batch(
            Batch(source_url=source_url, capability=capability, confidence=confidence)
        )
        jobs: list[Job] = []
        skipped: list[str] = []
        for item in items:
            url = getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None)
            if not url:
                continue
            title = getattr(item, "title", "") or (item.get("title", "") if isinstance(item, dict) else "")
            try:
                jobs.append(
                    self.submit(url, title=title, batch_id=batch.id, output_dir=output_dir)
                )
            except DuplicateJob as exc:
                logger.debug("skipping %s in batch %s: %s", url, batch.id, exc)
                skipped.append(url)
        return batch, jobs, skipped

    def _claim_output_path(self, job: Job, output_dir: Path | None) -> Path:
        from ..core.models import CaptureResult

        target_dir = output_dir or self.options.output_dir
        placeholder = CaptureResult(
            page_url=job.url,
            final_url=job.url,
            title=job.title,
            user_agent="",
            cookies={},
            requests=[],
        )
        return unique_path(
            output_path_for(placeholder, target_dir),
            taken=self.store.claimed_output_paths(),
        )

    # --- control -------------------------------------------------------------

    def cancel(self, job_id: str) -> bool:
        job = self.store.get(job_id)
        if job is None or job.status in {JobStatus.COMPLETED, JobStatus.CANCELLED}:
            return False

        with self._lock:
            event = self._cancels.get(job_id)
        if event is not None:
            event.set()

        if job.status == JobStatus.QUEUED:
            # Never started, so it can be finalized straight away.
            self._finish(job, JobStatus.CANCELLED)
        return True

    def retry(self, job_id: str) -> Job | None:
        job = self.store.get(job_id)
        if job is None:
            return None
        job.status = JobStatus.QUEUED
        job.error = None
        job.percent = None
        job.downloaded_bytes = None
        self.store.update(job)
        self._publish(job, PipelineEvent("job_queued", {"status": job.status.value}))
        self._pool.submit(self._run_job, job.id)
        return job

    def recover_interrupted(self) -> list[Job]:
        recovered = self.store.recover_interrupted()
        for job in recovered:
            self._publish(job, PipelineEvent("job_interrupted", {"status": job.status.value}))
        return recovered

    def shutdown(self, wait: bool = True) -> None:
        if self._closed:
            return
        self._closed = True
        with self._gate:
            for event in self._cancels.values():
                event.set()
            # Waiters must stop waiting, or the pool never drains.
            self._gate.notify_all()
        self._pool.shutdown(wait=wait)
        self.bus.close()

    # --- execution -----------------------------------------------------------

    def _run_job(self, job_id: str) -> None:
        if not self._admit():
            return
        try:
            job = self.store.get(job_id)
            if job is None or job.status in {JobStatus.CANCELLED, JobStatus.COMPLETED}:
                return

            cancel = threading.Event()
            with self._lock:
                self._cancels[job_id] = cancel

            try:
                self._execute(job, cancel)
            except DownloadCancelled:
                self._finish(job, JobStatus.CANCELLED)
            except Exception as exc:  # noqa: BLE001
                self._finish(job, JobStatus.FAILED, error=str(exc))
            finally:
                with self._lock:
                    self._cancels.pop(job_id, None)
        finally:
            self._release_slot()

    def _execute(self, job: Job, cancel: threading.Event) -> None:
        job.status = JobStatus.RESOLVING
        self.store.update(job)
        self._publish(job, PipelineEvent("job_started", {"status": job.status.value}))

        resolution = self.cache.get(job.resolution_id)
        if cancel.is_set():
            raise DownloadCancelled("cancelled before resolving")

        job.status = JobStatus.DOWNLOADING
        if resolution is not None:
            job.engine = resolution.engine or job.engine
            job.title = job.title or resolution.title
        self.store.update(job)

        runner = self.runner or self._default_runner
        out_file = runner(job, resolution, cancel, lambda event: self._on_event(job, event))

        job.output_path = str(out_file)
        self._finish(job, JobStatus.COMPLETED)

    def _default_runner(
        self,
        job: Job,
        resolution: Resolution | None,
        cancel: threading.Event,
        on_event: Callable[[PipelineEvent], None],
    ) -> Path:
        from ..engines.chain import ChainOptions, resolve as chain_resolve
        from ..engines.ytdlp_executor import YtDlpExecutor

        if resolution is None:
            resolutions = chain_resolve(job.url, ChainOptions())
            if not resolutions:
                raise RuntimeError("no engine resolved this URL")
            resolution = resolutions[0]
            job.engine = resolution.engine
            job.title = job.title or resolution.title

        wall = auth_wall_reason(job.url, resolution)
        if wall is not None:
            # Otherwise this surfaces as the misleading "no engine resolved this
            # URL", which reads as a broken extractor rather than a sign-in page.
            raise RuntimeError(f"this page requires signing in: {wall}")

        if job.output_path:
            out_file = Path(job.output_path)
        else:
            with self._claim_lock:
                out_file = self._claim_output_path(job, None)
        request = DownloadRequest(
            out_file=out_file,
            page_url=resolution.final_url or job.url,
            format_id=job.format_id,
            ffmpeg_location=self.options.ffmpeg_location,
        )

        if resolution.engine == "ytdlp":
            return YtDlpExecutor().run(request, cancel, on_event)

        return self._download_detected(resolution, request, cancel, on_event)

    def _download_detected(
        self,
        resolution: Resolution,
        request: DownloadRequest,
        cancel: threading.Event,
        on_event: Callable[[PipelineEvent], None],
    ) -> Path:
        """Download media the browser or a site plugin found.

        Discovery is delegated to `core.pipeline`, which is the path the CLI
        takes. A bare `detect_candidates` call is not enough: a page that serves
        its stream from an embed yields nothing on the outer capture, and only
        the pipeline's deep scan reaches the embed. The pipeline also re-captures
        that embed for the download, since the embed's own session is what its
        CDN authorizes, and its ranking probes the winner's duration, which is
        what lets progress report a percent instead of staying indeterminate.

        Only the transfer stays here, on the executor: that is what carries
        cancellation and the progress events.
        """
        from ..core.ffmpeg_executor import FfmpegExecutor
        from ..core.pipeline import (
            prepare_candidates,
            probe_duration_seconds,
            resolve_download_capture,
        )

        capture = capture_from_resolution(resolution)
        if not capture.requests:
            raise RuntimeError("the resolution carries no media to download")

        selected, _stage_counts, _all_candidates = prepare_candidates(capture, self.options, on_event)
        if not selected:
            raise RuntimeError("no candidate could be detected for this resolution")
        if cancel.is_set():
            raise DownloadCancelled("cancelled before downloading")

        download_capture = resolve_download_capture(capture, selected, self.options, on_event)

        attempts = min(self.options.max_attempts, len(selected))
        last_error: Exception | None = None
        for index, candidate in enumerate(selected[:attempts], start=1):
            if cancel.is_set():
                raise DownloadCancelled("cancelled between candidates")
            on_event(
                PipelineEvent(
                    CANDIDATE_ATTEMPT,
                    {
                        "index": index,
                        "total": attempts,
                        "kind": candidate.kind,
                        "url": candidate.url,
                    },
                )
            )
            attempt = replace(request, capture=download_capture, candidate=candidate)
            try:
                out_file = FfmpegExecutor(duration_hint=candidate.probe_duration).run(
                    attempt, cancel, on_event
                )
            except DownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                on_event(
                    PipelineEvent(
                        CANDIDATE_REJECTED,
                        {"url": candidate.url, "reason": "error", "error": str(exc)},
                    )
                )
                continue

            duration = probe_duration_seconds(out_file)
            if duration is not None and duration < float(self.options.min_duration):
                # Usually an advert or a preview rather than the feature.
                on_event(
                    PipelineEvent(
                        CANDIDATE_REJECTED,
                        {
                            "url": candidate.url,
                            "reason": "too_short",
                            "duration": duration,
                            "minimum": self.options.min_duration,
                        },
                    )
                )
                try:
                    out_file.unlink(missing_ok=True)
                except OSError:
                    pass
                continue

            return out_file

        if last_error is not None:
            raise RuntimeError(f"every candidate failed to download: {last_error}")
        raise RuntimeError("every candidate was rejected as too short")

    # --- event plumbing ------------------------------------------------------

    def _on_event(self, job: Job, event: PipelineEvent) -> None:
        if event.kind == PROGRESS:
            payload = event.payload
            job.phase = payload.get("phase", job.phase)
            job.percent = payload.get("percent")
            job.downloaded_bytes = payload.get("downloaded_bytes")
            job.total_bytes = payload.get("total_bytes")
            job.speed_bps = payload.get("speed_bps")
            job.eta_seconds = payload.get("eta_seconds")
            self.store.update(job)
        elif event.kind == DOWNLOAD_COMPLETED:
            job.output_path = event.payload.get("path", job.output_path)
        elif event.kind == FAILED:
            job.error = event.payload.get("error") or job.error
        self._publish(job, event)

    def _finish(self, job: Job, status: JobStatus, error: str | None = None) -> None:
        job.status = status
        if error is not None:
            job.error = error
        if status == JobStatus.COMPLETED:
            job.percent = 100.0
        self.store.update(job)
        self._publish(job, PipelineEvent(f"job_{status.value}", {"status": status.value, "error": job.error}))

    def _publish(self, job: Job, event: PipelineEvent) -> None:
        from .models import JobEvent
        from .store import utcnow

        self._log_event(job, event)
        self.bus.publish(JobEvent(job_id=job.id, batch_id=job.batch_id, event=event, created_at=utcnow()))

    def _log_event(self, job: Job, event: PipelineEvent) -> None:
        """Mirror an event to the server log.

        The event stream only reaches a connected client. An operator watching
        the console had no way to tell a multi-minute deep scan from a hang, so
        the same events are written there too.

        Progress is throttled: FFmpeg reports several times a second, and a wall
        of near-identical lines hides the messages that actually say what stage
        the work is in.
        """
        short = job.id[:8]
        if event.kind == PROGRESS:
            now = time.monotonic()
            last = self._last_progress_log.get(job.id, 0.0)
            if now - last < PROGRESS_LOG_INTERVAL_SECONDS:
                return
            self._last_progress_log[job.id] = now
            percent = event.payload.get("percent")
            where = event.payload.get("phase") or job.phase
            shown = f"{percent:.1f}%" if isinstance(percent, (int, float)) else "unknown"
            logger.info("job %s %s: %s", short, where, shown)
            return

        message = event.payload.get("message") or event.payload.get("error") or ""
        detail = f" - {message}" if message else ""
        logger.info("job %s %s%s", short, event.kind, detail)
