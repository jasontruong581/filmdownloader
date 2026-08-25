---
phase: 3
title: Job store and progress events
status: planned
priority: P1
effort: 9h
dependencies: [1b, 2]
order: 4
---

# Phase 3: Job store and progress events

## Context

Progress today exists only as FFmpeg's stderr on the terminal
(`subprocess.run(cmd, capture_output=False)`), which also discards the stderr
that would explain a failure. Queue state lives in a CSV that
`batch_run_csv.py` rewrites row by row, and its skip-check globs
`{video_id}-*.mp4`, so output naming is load-bearing for resume. Neither can
drive a web UI.

This is the hardest phase. It carries the FFmpeg progress rewrite, Windows
cancellation semantics, and the resolution cache the UI's two-step
resolve-then-queue flow depends on.

## Requirements

- Functional: a job records id, url, engine, `resolution_id`, chosen format,
  output path, status, phase, nullable percent, nullable byte totals, speed, eta,
  error, batch id, timestamps.
- Functional: statuses are `queued`, `resolving`, `downloading`,
  `postprocessing`, `completed`, `failed`, `cancelled`, `interrupted`.
- Functional: **the resolve-to-queue handoff must not re-resolve blindly.** For a
  yt-dlp URL a re-resolve is a wasted extract; for a browser-capture URL it is a
  second full Chrome session of 30-60 seconds per job, and captured media URLs
  often carry short-lived tokens so caching them too long fails too.
- Functional: jobs persist across restarts. A job left `downloading` when the
  process died becomes `interrupted` and is resumable, never reported complete.
- Functional: a running job can be cancelled, terminating the FFmpeg or yt-dlp
  work and removing the partial file.
- Functional: batch queueing creates N independent jobs sharing a `batch_id`.
  One item failing never fails the batch.
- Functional: concurrency is bounded, configurable, and takes effect without a
  restart.
- Functional: duplicate protection. Submitting a URL and format that is already
  active is rejected rather than racing on the same output path.
- Functional: an existing `links.csv` from `crawl-links` imports as queued jobs.
- Non-functional: the store is SQLite via stdlib `sqlite3`, WAL journal mode, one
  write lock, short busy timeout. No ORM.
- Non-functional: state lives in `data/`, **not** in the media output directory,
  because the setting naming the output directory must not live inside the
  directory it names.
- Non-functional: job tests run without FFmpeg, yt-dlp, or Chrome via an
  injected fake executor.

## Related code files

- Create: `src/videotrack/jobs/__init__.py`, `jobs/models.py`, `jobs/store.py`,
  `jobs/manager.py`, `jobs/bus.py`, `jobs/cache.py`
- Modify: `src/videotrack/core/download.py` (progress, cancellation, stderr
  retention), `core/executor.py`, `engines/ytdlp_executor.py`, `cli.py`,
  `batch_run_csv.py`
- Create: `tests/test_job_store.py`, `tests/test_job_manager.py`,
  `tests/test_ffmpeg_progress.py`, `tests/test_resolution_cache.py`

## Implementation steps

1. `jobs/models.py`: `Job`, `JobStatus`, and a `JobEvent` that wraps the
   `PipelineEvent` vocabulary defined in phase 1b. The vocabulary is not
   redefined here; reusing it is what keeps the UI engine-agnostic.
2. `jobs/store.py`: SQLite with a `schema_version` table, a `jobs` table, and a
   `batches` table. Open `check_same_thread=False`, guard writes with a lock,
   enable WAL, set a busy timeout. Default path `data/jobs.db`, overridable by
   `FILMDOWNLOADER_DB`. Add `data/` to `.gitignore`.
3. `jobs/cache.py`: a TTL cache mapping `resolution_id` to a `Resolution`.
   `POST /api/resolve` populates it; `POST /api/jobs` accepts either a
   `resolution_id` (fast path, no re-resolve) or a bare `url` (the worker
   resolves in-job). The worker re-resolves **only** when the cached media URL
   fails at first byte, which covers expired tokens without paying an
   unconditional second capture. Batch queueing uses the bare-url path, since
   enumerated items were never individually resolved.
4. `jobs/bus.py`: an `EventBus` with `publish(event)` and `subscribe()` returning
   a queue. **No per-job replay ring buffer.** Phase 5 reconciles against
   `GET /api/jobs` on mount and on every reconnect, and that snapshot already
   guarantees the UI cannot go stale, so a replay buffer is a redundant second
   consistency surface. SSE carries live events; REST carries truth.
5. Rewrite FFmpeg invocation in `core/download.py`:
   - add `-nostats -progress pipe:1`;
   - `subprocess.Popen(stdout=PIPE, stderr=PIPE)`, reading the key/value progress
     block line by line;
   - derive percent from `out_time_us` against the ffprobe duration, emitting
     `percent=None` when no duration is available rather than faking zero;
   - retain the last N stderr lines for the failure message, replacing today's
     bare "ffmpeg failed";
   - catch `FileNotFoundError` and report "ffmpeg not found at <path>";
   - accept `cancel: threading.Event`; on set, `terminate()`, then `kill()` after
     a grace period, then unlink the partial output.
6. Map yt-dlp `progress_hooks` onto the same event shape using the phase-2
   normalization, including the multi-file `phase` field so a split-format
   download reports one monotonic track.
7. `jobs/manager.py`: `JobManager` over a `ThreadPoolExecutor`, with
   `submit(...)`, `submit_batch(items) -> batch_id`, `cancel(job_id)`,
   `retry(job_id)`, and `recover_interrupted()` at startup. Threads rather than
   asyncio, because the work is blocking subprocess and requests code; FastAPI
   bridges with `run_in_threadpool`. Gate concurrency with a **semaphore** rather
   than the pool size, since a `ThreadPoolExecutor` cannot shrink and the
   setting must apply without a restart.
8. Cancellation honesty: `capture_page` has no interruption hook. Cancel takes
   effect between pipeline stages and before each candidate attempt; an
   in-flight Chrome capture completes first. State this in the job model
   documentation and surface it in the UI as "cancelling..." rather than
   pretending it is immediate.
9. Output path assignment moves into `submit()` so collision handling and
   duplicate-active rejection happen once, before a worker starts, rather than
   racing inside two concurrent FFmpeg processes.
10. Keep `batch_run_csv.py` working. Its `has_downloaded_file` glob is
    load-bearing for resume, so either preserve `{page_id}-{video_code}` naming
    for plugin-metadata sites or migrate the skip-check to query the job store.
    Migrating is preferred; whichever is chosen, an existing CSV must stay
    readable and must not silently re-download.
11. Add `python main.py queue add|list|cancel|retry|import-csv|batch` so the job
    layer is exercisable before the API exists.

## Validation

- `tests/test_job_store.py`: round-trip; a `downloading` job reloaded through
  `recover_interrupted()` returns as `interrupted`; the schema upgrade runs once;
  concurrent writes from two threads do not raise "database is locked".
- `tests/test_job_manager.py`: with a fake executor, concurrency never exceeds
  the limit; lowering the setting takes effect without a restart; `cancel()`
  yields `cancelled` and the fake sees its cancel event; a raising executor
  yields `failed` carrying the error text; `submit_batch` of 5 items where 2 fail
  leaves 3 `completed` and the batch reported partial.
- `tests/test_ffmpeg_progress.py`: recorded progress lines parse to the expected
  percent, bytes, and speed; a stream with no duration yields `percent=None`;
  retained stderr appears in the failure message. No FFmpeg needed.
- `tests/test_resolution_cache.py`: a `resolution_id` hit skips re-resolution
  (assert the chain is not called); an expired id falls back to a URL resolve; a
  first-byte failure on a cached URL triggers exactly one re-resolve.
- Two jobs submitted with identical derived titles produce two distinct files.
- Submitting the same URL and format twice while the first is active is rejected.
- Kill the process mid-download, restart, confirm `interrupted`, then retry.

## Risk

Windows cancellation can leave a locked partial file. Mitigation: write to
`.part` and rename only on success, the pattern `sites/flowplayer.py` already
uses; tolerate an unlink failure by scheduling the delete at next startup.

The resolution cache is a correctness hazard in both directions: too long and
tokens expire, too short and browser captures are repeated. Mitigation: short
TTL plus the first-byte-failure re-resolve, tested explicitly.

## Rollback

The job layer is additive; `cmd_run` works without it. Reverting means dropping
`jobs/` and the `queue` subcommand and restoring `batch_run_csv.py`.
