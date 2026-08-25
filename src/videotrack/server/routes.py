"""HTTP routes.

Thin adapters over the engine chain, the batch probe, the job manager, and the
store. No business logic lives here; each body is a handful of lines.
"""

from __future__ import annotations

import json
import mimetypes
import queue as queue_module
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse

from ..core.models import BatchItem
from ..core.preflight import check_tools
from ..engines import ytdlp_version
from ..engines.batch import probe as batch_probe
from ..engines.batch import sample_verify
from ..engines.chain import resolve as chain_resolve
from ..jobs.manager import DuplicateJob
from ..jobs.models import Job
from ..sites import plugin_names
from .schemas import (
    BatchOut,
    BatchProbeIn,
    BatchProbeOut,
    BatchQueueIn,
    BatchVerifyIn,
    BatchVerifyOut,
    FormatOut,
    HealthOut,
    JobCreateIn,
    JobOut,
    LibraryItemOut,
    ResolveIn,
    ResolveOut,
    SettingsIn,
    SettingsOut,
    ToolStatusOut,
)
from .settings import EDITABLE_FIELDS, Settings

router = APIRouter()

#: Media extensions the library lists. Sidecars and partials are excluded.
LIBRARY_EXTENSIONS = frozenset({".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".ts", ".mov"})

#: Bytes per chunk when serving a ranged request.
RANGE_CHUNK = 1024 * 256

SSE_HEARTBEAT_SECONDS = 15.0

# Spelled out rather than taken from starlette.status: those names were
# renamed and the old spellings now emit deprecation warnings.
HTTP_416_RANGE_NOT_SATISFIABLE = 416
HTTP_422_UNPROCESSABLE_CONTENT = 422


def _state(request: Request):
    return request.app.state.server


def _fail(code: int, reason: str, message: str) -> HTTPException:
    """A machine-readable refusal, never a stack trace."""
    return HTTPException(status_code=code, detail={"reason": reason, "message": message})


# --- health ------------------------------------------------------------------


@router.get("/health", response_model=HealthOut)
async def health(request: Request) -> HealthOut:
    state = _state(request)
    statuses = await run_in_threadpool(check_tools, state.settings.resolved_ffmpeg_location)
    tools = [
        ToolStatusOut(
            name=item.name,
            required=item.required,
            available=item.available,
            path=item.path,
            version=item.version,
        )
        for item in statuses
    ]
    return HealthOut(
        ok=not any(item.blocking for item in statuses),
        tools=tools,
        ytdlp_version=ytdlp_version(),
        free_bytes=state.free_bytes(),
        output_dir=str(state.settings.resolved_output_dir),
        plugins=list(plugin_names()),
    )


# --- resolve -----------------------------------------------------------------


def _format_out(fmt) -> FormatOut:
    return FormatOut(
        format_id=fmt.format_id,
        label=fmt.label(),
        track=fmt.track,
        container=fmt.container,
        height=fmt.height,
        fps=fmt.fps,
        vcodec=fmt.vcodec,
        acodec=fmt.acodec,
        filesize=fmt.best_effort_size,
        tbr=fmt.tbr,
    )


@router.post("/resolve", response_model=ResolveOut)
async def resolve(payload: ResolveIn, request: Request) -> ResolveOut:
    state = _state(request)
    if not state.resolve_slots.acquire(blocking=False):
        raise _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "resolve_busy",
            "Too many resolutions in flight. Try again shortly.",
        )
    try:
        resolutions = await run_in_threadpool(
            chain_resolve, payload.url, state.chain_options(payload.engines)
        )
    finally:
        state.resolve_slots.release()

    if not resolutions:
        raise _fail(
            HTTP_422_UNPROCESSABLE_CONTENT,
            "not_resolved",
            "No engine recognized media on this page.",
        )

    resolution = resolutions[0]
    resolution_id = state.cache.put(resolution)
    return ResolveOut(
        resolution_id=resolution_id,
        engine=resolution.engine,
        url=payload.url,
        final_url=resolution.final_url,
        title=resolution.title,
        duration=resolution.duration,
        thumbnail=resolution.thumbnail,
        uploader=resolution.uploader,
        formats=[_format_out(fmt) for fmt in resolution.formats],
        item_count=len(resolutions),
    )


# --- batch -------------------------------------------------------------------


@router.post("/batch/probe", response_model=BatchProbeOut)
async def probe_batch(payload: BatchProbeIn, request: Request) -> BatchProbeOut:
    state = _state(request)
    if not state.resolve_slots.acquire(blocking=False):
        raise _fail(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "resolve_busy",
            "Too many probes in flight. Try again shortly.",
        )
    try:
        result = await run_in_threadpool(batch_probe, payload.url)
    finally:
        state.resolve_slots.release()

    # A negative answer is a normal answer here: the UI renders the reason and
    # keeps the batch control disabled.
    return BatchProbeOut(
        capability=result.capability,
        confidence=result.confidence,
        batchable=result.is_batchable,
        items=[{"url": item.url, "title": item.title} for item in result.items],
        total_estimate=result.total_estimate,
        truncated=result.truncated,
        reason=result.reason,
    )


@router.post("/batch/verify", response_model=BatchVerifyOut)
async def verify_batch(payload: BatchVerifyIn) -> BatchVerifyOut:
    items = tuple(BatchItem(url=item.url, title=item.title) for item in payload.items)
    verified, attempted = await run_in_threadpool(sample_verify, items, payload.count)
    return BatchVerifyOut(verified=verified, attempted=attempted)


@router.post("/batch/jobs", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
async def queue_batch(payload: BatchQueueIn, request: Request) -> BatchOut:
    state = _state(request)
    if not payload.items:
        raise _fail(status.HTTP_400_BAD_REQUEST, "no_items", "No items were selected.")

    items = tuple(BatchItem(url=item.url, title=item.title) for item in payload.items)
    batch, jobs, skipped = await run_in_threadpool(
        state.manager.submit_batch,
        items,
        source_url=payload.source_url,
        capability=payload.capability,
        confidence=payload.confidence,
    )
    return BatchOut(
        id=batch.id,
        source_url=batch.source_url,
        capability=batch.capability,
        confidence=batch.confidence,
        created_at=batch.created_at,
        jobs=[JobOut(**job.to_dict()) for job in jobs],
        skipped=skipped,
    )


@router.get("/batches/{batch_id}", response_model=BatchOut)
async def get_batch(batch_id: str, request: Request) -> BatchOut:
    state = _state(request)
    batch = state.store.get_batch(batch_id)
    if batch is None:
        raise _fail(status.HTTP_404_NOT_FOUND, "unknown_batch", "No such batch.")
    jobs = state.store.list(batch_id=batch_id)
    return BatchOut(
        id=batch.id,
        source_url=batch.source_url,
        capability=batch.capability,
        confidence=batch.confidence,
        created_at=batch.created_at,
        jobs=[JobOut(**job.to_dict()) for job in jobs],
        skipped=[],
    )


# --- jobs --------------------------------------------------------------------


def _job_out(job: Job) -> JobOut:
    return JobOut(**job.to_dict())


@router.post("/jobs", response_model=JobOut, status_code=status.HTTP_201_CREATED)
async def create_job(payload: JobCreateIn, request: Request) -> JobOut:
    state = _state(request)

    url = payload.url
    title = payload.title
    engine = ""
    if payload.resolution_id:
        resolution = state.cache.get(payload.resolution_id)
        if resolution is None and not url:
            raise _fail(
                status.HTTP_410_GONE,
                "resolution_expired",
                "That resolution has expired. Resolve the URL again.",
            )
        if resolution is not None:
            url = url or resolution.final_url or resolution.page_url
            title = title or resolution.title
            engine = resolution.engine

    if not url:
        raise _fail(status.HTTP_400_BAD_REQUEST, "no_url", "A url or a valid resolution_id is required.")

    try:
        job = await run_in_threadpool(
            state.manager.submit,
            url,
            resolution_id=payload.resolution_id,
            format_id=payload.format_id or state.settings.default_format or None,
            title=title,
            engine=engine,
        )
    except DuplicateJob as exc:
        raise _fail(status.HTTP_409_CONFLICT, "duplicate_job", str(exc)) from exc
    return _job_out(job)


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    request: Request,
    job_status: str = Query("", alias="status"),
    batch_id: str = Query(""),
    limit: int = Query(500, ge=1, le=5000),
) -> list[JobOut]:
    state = _state(request)
    jobs = state.store.list(status=job_status or None, batch_id=batch_id or None, limit=limit)
    return [_job_out(job) for job in jobs]


@router.get("/jobs/events")
async def job_events(request: Request) -> StreamingResponse:
    """Live events only. Clients reconcile against GET /jobs on connect."""
    state = _state(request)
    subscriber = state.bus.subscribe()

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    return
                event = await run_in_threadpool(_next_event, subscriber)
                if event is None:
                    # Idle: a comment keeps proxies and browsers from closing.
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(event.to_dict())}\n\n"
        finally:
            state.bus.unsubscribe(subscriber)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _next_event(subscriber):
    """Block for the next event, or return None so the caller can heartbeat."""
    try:
        return subscriber.get(timeout=SSE_HEARTBEAT_SECONDS)
    except queue_module.Empty:
        return None


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str, request: Request) -> JobOut:
    job = _state(request).store.get(job_id)
    if job is None:
        raise _fail(status.HTTP_404_NOT_FOUND, "unknown_job", "No such job.")
    return _job_out(job)


@router.delete("/jobs/{job_id}", response_model=JobOut)
async def cancel_job(job_id: str, request: Request) -> JobOut:
    state = _state(request)
    if not await run_in_threadpool(state.manager.cancel, job_id):
        raise _fail(status.HTTP_409_CONFLICT, "not_cancellable", "That job cannot be cancelled.")
    job = state.store.get(job_id)
    if job is None:
        raise _fail(status.HTTP_404_NOT_FOUND, "unknown_job", "No such job.")
    return _job_out(job)


@router.post("/jobs/{job_id}/retry", response_model=JobOut)
async def retry_job(job_id: str, request: Request) -> JobOut:
    state = _state(request)
    job = await run_in_threadpool(state.manager.retry, job_id)
    if job is None:
        raise _fail(status.HTTP_404_NOT_FOUND, "unknown_job", "No such job.")
    return _job_out(job)


# --- library -----------------------------------------------------------------


def _library_root(request: Request) -> Path:
    return _state(request).settings.resolved_output_dir.resolve()


def _safe_library_path(root: Path, item_id: str) -> Path:
    """Resolve an id inside the output directory, rejecting any escape.

    A crafted id must not be able to read arbitrary files, so the resolved path
    is required to stay under the root.
    """
    if not item_id or "\x00" in item_id:
        raise _fail(status.HTTP_404_NOT_FOUND, "unknown_item", "No such file.")
    candidate = (root / item_id).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise _fail(status.HTTP_404_NOT_FOUND, "unknown_item", "No such file.") from None
    if not candidate.is_file():
        raise _fail(status.HTTP_404_NOT_FOUND, "unknown_item", "No such file.")
    return candidate


@router.get("/library", response_model=list[LibraryItemOut])
async def list_library(request: Request) -> list[LibraryItemOut]:
    root = _library_root(request)
    if not root.exists():
        return []
    items = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in LIBRARY_EXTENSIONS:
            continue
        if path.stem.endswith(".part"):
            continue
        stat = path.stat()
        items.append(
            LibraryItemOut(
                id=path.relative_to(root).as_posix(),
                name=path.name,
                size_bytes=stat.st_size,
                modified_at=stat.st_mtime,
            )
        )
    items.sort(key=lambda item: item.modified_at, reverse=True)
    return items


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes=") :].split(",", 1)[0].strip()
    start_text, _, end_text = spec.partition("-")
    try:
        if not start_text:
            length = int(end_text)
            if length <= 0:
                return None
            return max(size - length, 0), size - 1
        start = int(start_text)
        end = int(end_text) if end_text else size - 1
    except ValueError:
        return None
    if start >= size or start < 0:
        return None
    return start, min(end, size - 1)


@router.get("/library/{item_id:path}/file")
async def get_library_file(item_id: str, request: Request) -> Response:
    root = _library_root(request)
    path = _safe_library_path(root, item_id)
    size = path.stat().st_size
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    range_header = request.headers.get("range", "")
    if not range_header:
        return FileResponse(path, media_type=media_type, filename=path.name)

    parsed = _parse_range(range_header, size)
    if parsed is None:
        return Response(
            status_code=HTTP_416_RANGE_NOT_SATISFIABLE,
            headers={"Content-Range": f"bytes */{size}"},
        )

    start, end = parsed

    def chunks():
        with path.open("rb") as handle:
            handle.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                block = handle.read(min(RANGE_CHUNK, remaining))
                if not block:
                    return
                remaining -= len(block)
                yield block

    return StreamingResponse(
        chunks(),
        status_code=status.HTTP_206_PARTIAL_CONTENT,
        media_type=media_type,
        headers={
            "Content-Range": f"bytes {start}-{end}/{size}",
            "Content-Length": str(end - start + 1),
            "Accept-Ranges": "bytes",
        },
    )


# --- settings ----------------------------------------------------------------


@router.get("/settings", response_model=SettingsOut)
async def get_settings(request: Request) -> SettingsOut:
    return SettingsOut(**_state(request).settings.to_dict())


@router.put("/settings", response_model=SettingsOut)
async def put_settings(payload: SettingsIn, request: Request) -> SettingsOut:
    state = _state(request)
    current = state.settings
    updates = payload.model_dump(exclude_none=True)

    merged = Settings(**{**current.__dict__})
    for field in EDITABLE_FIELDS:
        if field in updates:
            setattr(merged, field, updates[field])

    if merged.concurrency < 1:
        raise _fail(status.HTTP_400_BAD_REQUEST, "bad_concurrency", "Concurrency must be at least 1.")

    applied = await run_in_threadpool(state.apply_settings, merged)
    return SettingsOut(**applied.to_dict())
