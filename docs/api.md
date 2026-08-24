# HTTP API

Base path `/api`. The generated schema is authoritative and browsable at `/docs`
while the server runs; this page covers intent and the parts a schema cannot
express. Dump the schema without starting a server:

```bash
python -m videotrack.server --dump-openapi openapi.json
```

## Conventions

**Measurements are nullable.** `percent`, `total_bytes`, `duration`, and
`filesize` are absent when genuinely unknown. Render them as unknown, not as
zero: a bar pinned at zero reads as a stalled download.

**Refusals are machine-readable.** Any 4xx carries
`{"detail": {"reason": "...", "message": "..."}}`. Branch on `reason`; show
`message`.

| Reason | Status | Meaning |
|---|---|---|
| `not_resolved` | 422 | No engine recognized media on the page |
| `resolve_busy` | 429 | Resolve concurrency saturated; retry shortly |
| `no_url` | 400 | Neither a url nor a usable resolution_id was given |
| `resolution_expired` | 410 | The cached resolution aged out; resolve again |
| `duplicate_job` | 409 | An active job exists for this URL and format |
| `not_cancellable` | 409 | The job is already finished, or unknown |
| `unknown_job` / `unknown_batch` / `unknown_item` | 404 | No such record |
| `no_items` | 400 | A batch was queued with nothing selected |
| `bad_concurrency` | 400 | Concurrency below 1 |
| `host_not_allowed` | 400 | `Host` header is not a loopback name |
| `token_required` / `token_invalid` | 401 | Token missing or wrong |
| `unknown_endpoint` | 404 | No such API path |

**Authentication** applies only when the server is bound off loopback. Send
`Authorization: Bearer <token>`, or `?access_token=<token>` where headers are
impossible, which is the case for `EventSource`.

## Health

`GET /health`

Tool availability by resolved path, the yt-dlp version, free space on the output
directory, and the registered plugins. `ok` is false when a required tool is
missing. Check this before queueing: FFmpeg is commonly absent from `PATH` on
Windows, and finding out at download time is worse.

## Resolve

`POST /resolve` with `{"url": "...", "engines": ["ytdlp"]}` (engines optional).

Returns the winning engine, title, duration, thumbnail, selectable formats, and a
`resolution_id`. **Pass that id to `POST /jobs`** so queueing does not resolve
again: for a browser-resolved page that saves a second full Chrome session.

`item_count` above 1 means the URL enumerated several items; use the batch
endpoints to queue them all.

## Batch

`POST /batch/probe` with `{"url": "..."}`

Returns `capability` (`playlist` | `collection` | `crawl` | `none`),
`confidence` (`proven` | `possible` | `none`), `batchable`, the enumerated
`items`, `truncated`, and `reason`.

Gate the UI on this, not on the URL. `possible` means page links rather than
confirmed media and warrants an explicit confirmation. A probe proves enumeration,
not downloadability. See [batch-capability.md](batch-capability.md).

`POST /batch/verify` with `{"items": [...], "count": 2}`

Fully resolves the first `count` items and reports `verified` of `attempted`.

`POST /batch/jobs` with `{"items": [...], "source_url": "...", "capability": "...", "confidence": "..."}`

Creates one job per item under a shared `batch_id`. `skipped` lists URLs that
already had an active job. One item failing never affects the others.

`GET /batches/{id}` returns the batch with its jobs.

## Jobs

| Route | Purpose |
|---|---|
| `POST /jobs` | Queue one download. Body takes `resolution_id` and/or `url`, plus optional `format_id` and `title`. |
| `GET /jobs` | List, with optional `status`, `batch_id`, `limit`. |
| `GET /jobs/{id}` | One job. |
| `DELETE /jobs/{id}` | Cancel. |
| `POST /jobs/{id}/retry` | Requeue a failed, cancelled, or interrupted job. |

Statuses: `queued`, `resolving`, `downloading`, `postprocessing`, `completed`,
`failed`, `cancelled`, `interrupted`.

`interrupted` means the process died while the job was running. It is never
reported as complete and is safe to retry.

Cancellation is not always instantaneous. FFmpeg and yt-dlp stop promptly, but a
browser capture has no interruption hook, so cancellation takes effect between
pipeline stages. Show "cancelling" rather than implying it is immediate.

## Events

`GET /jobs/events` is a Server-Sent Events stream. Each frame is
`data: {job_id, batch_id, created_at, kind, payload}`.

Event kinds: `job_queued`, `job_started`, `job_completed`, `job_failed`,
`job_cancelled`, `job_interrupted`, `progress`, `download_completed`,
`candidate_attempt`, `candidate_rejected`, `candidates_found`, `stage_started`,
`failed`, `info`.

A `progress` payload carries `phase`, `percent`, `downloaded_bytes`,
`total_bytes`, `speed_bps`, `eta_seconds`. Phases are `downloading`,
`downloading:video`, `downloading:audio`, `merging`, `postprocessing`. The
backend already folds a multi-file download into one monotonic track, so render
`percent` directly.

**The stream carries live events only, with no replay.** Fetch `GET /jobs` on
connect and on every reconnect; that snapshot is what guarantees a missed event
cannot leave a stale view, and it is why a replay buffer would be a redundant
second consistency mechanism. A comment heartbeat arrives every 15 seconds so
proxies and browsers hold the connection.

## Library

`GET /library` lists completed media with `id`, `name`, `size_bytes`, and
`modified_at`. Sidecars and partial files are excluded, and capture artifacts are
never listed.

`GET /library/{id}/file` serves one file and honors `Range`, so a browser can
seek. `id` is a path relative to the output directory; it is resolved against
that directory and rejected if it escapes.

## Settings

`GET /settings`, `PUT /settings`.

Editable: `output_dir`, `concurrency`, `engines`, `default_format`,
`ffmpeg_location`, `cookies_from_browser`.

`concurrency` applies immediately. `host` and `port` are reported but not
editable: changing where the server listens is not a runtime operation.

`cookies_from_browser` is off by default and best effort; current Chrome's
app-bound cookie encryption can defeat it. It reuses a session the operator
already holds and bypasses no access control.
