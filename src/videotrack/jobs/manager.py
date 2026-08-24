"""The worker pool.

Threads rather than asyncio: the work is blocking subprocess and requests code,
and a server bridges with `run_in_threadpool`.

Concurrency is gated by a semaphore rather than the pool size, because a
`ThreadPoolExecutor` cannot shrink and the setting has to apply without a
restart.

Cancellation honesty, stated once here because it reaches the UI: a browser
capture has no interruption hook. Cancel takes effect between pipeline stages and
before each candidate attempt, so an in-flight Chrome capture finishes first. The
UI says "cancelling" rather than pretending it is immediate.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.download import output_path_for, unique_path
from ..core.events import (
    DOWNLOAD_COMPLETED,
    FAILED,
    PROGRESS,
    PipelineEvent,
)
from ..core.executor import DownloadCancelled, DownloadRequest
from ..core.options import PipelineOptions
from ..core.resolvers import Resolution, capture_from_resolution
from .bus import EventBus
from .cache import ResolutionCache
from .models import Batch, Job, JobStatus
from .store import JobStore

DEFAULT_CONCURRENCY = 2
MAX_POOL_WORKERS = 16

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
        self._semaphore = threading.BoundedSemaphore(MAX_POOL_WORKERS)
        self._permits = MAX_POOL_WORKERS
        self._cancels: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=MAX_POOL_WORKERS, thread_name_prefix="job")
        self._closed = False
        self.set_concurrency(self.concurrency)

    # --- concurrency ---------------------------------------------------------

    def set_concurrency(self, value: int) -> None:
        """Change how many jobs run at once, effective immediately.

        Implemented by holding back permits on a fixed-size pool, since a
        ThreadPoolExecutor cannot be resized after construction.
        """
        target = max(1, min(int(value), MAX_POOL_WORKERS))
        with self._lock:
            while self._permits > target:
                self._semaphore.acquire()
                self._permits -= 1
            while self._permits < target:
                self._semaphore.release()
                self._permits += 1
            self.concurrency = target

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
        # The output path is claimed before a worker starts, so two concurrent
        # jobs cannot resolve the same name and race on the same file.
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
        return unique_path(output_path_for(placeholder, target_dir))

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
        with self._lock:
            for event in self._cancels.values():
                event.set()
        self._pool.shutdown(wait=wait)
        self.bus.close()

    # --- execution -----------------------------------------------------------

    def _run_job(self, job_id: str) -> None:
        with self._semaphore:
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
        from ..core.ffmpeg_executor import FfmpegExecutor
        from ..engines.chain import ChainOptions, resolve as chain_resolve
        from ..engines.ytdlp_executor import YtDlpExecutor

        if resolution is None:
            resolutions = chain_resolve(job.url, ChainOptions())
            if not resolutions:
                raise RuntimeError("no engine resolved this URL")
            resolution = resolutions[0]
            job.engine = resolution.engine
            job.title = job.title or resolution.title

        out_file = Path(job.output_path) if job.output_path else self._claim_output_path(job, None)
        request = DownloadRequest(
            out_file=out_file,
            page_url=resolution.final_url or job.url,
            format_id=job.format_id,
            ffmpeg_location=self.options.ffmpeg_location,
        )

        if resolution.engine == "ytdlp":
            return YtDlpExecutor().run(request, cancel, on_event)

        capture = capture_from_resolution(resolution)
        if not capture.requests:
            raise RuntimeError("the resolution carries no media to download")
        from ..core.detect import detect_candidates

        candidates = detect_candidates(capture, probe=False, host_bonuses=self.options.host_bonuses)
        if not candidates:
            raise RuntimeError("no candidate could be detected for this resolution")
        request.capture = capture
        request.candidate = candidates[0]
        return FfmpegExecutor().run(request, cancel, on_event)

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

        self.bus.publish(JobEvent(job_id=job.id, batch_id=job.batch_id, event=event, created_at=utcnow()))
