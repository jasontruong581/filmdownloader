---
phase: 4
title: FastAPI backend
status: planned
priority: P1
effort: 7h
dependencies: [3]
order: 5
---

# Phase 4: FastAPI backend

## Context

The core, engines, and job layer are callable but terminal-only. This phase adds
the HTTP surface the SPA consumes, and nothing more: no business logic lives in
the routes.

## Requirements

- Functional: `POST /api/resolve` runs the chain and returns title, duration,
  thumbnail, winning engine, the selectable formats, and a `resolution_id` that
  `POST /api/jobs` can consume so queueing does not re-resolve.
- Functional: `POST /api/batch/probe` returns the phase-2 `BatchProbe`:
  capability, confidence, enumerated items, truncation flag, and a reason when
  nothing was found. `POST /api/batch/verify` optionally sample-resolves the
  first n items.
- Functional: `POST /api/batch/jobs` queues selected items as N jobs under one
  `batch_id`; `GET /api/batches/{id}` reports per-item status.
- Functional: `POST /api/jobs` queues one download; `GET /api/jobs` lists with
  status filter; `GET /api/jobs/{id}`; `DELETE /api/jobs/{id}` cancels;
  `POST /api/jobs/{id}/retry`.
- Functional: `GET /api/jobs/events` streams events as SSE, and
  `GET /api/jobs/{id}/events` for one job. SSE rather than WebSocket: the stream
  is one-directional and reconnects on its own.
- Functional: `GET /api/library` lists completed output files;
  `GET /api/library/{id}/file` streams one with `Range` support.
- Functional: `GET /api/settings` and `PUT /api/settings` expose output
  directory, concurrency, engine order, default format preference,
  `ffmpeg_location`, and `cookies_from_browser`.
- Functional: `GET /api/health` returns the preflight result plus free disk space
  on the output directory, so the UI can warn before anything is queued.
- Non-functional: binds loopback `127.0.0.1:8756` by default. A wider bind is
  opt-in and requires `FILMDOWNLOADER_TOKEN` on every `/api` request; a
  non-loopback bind with no token **refuses to start**.
- Non-functional: **DNS-rebinding guard.** Loopback plus no token is not safe on
  its own: a malicious page can rebind its own hostname to 127.0.0.1 and gain
  same-origin access to this API, which queues arbitrary downloads and writes
  files. Reject any request whose `Host` header is not
  `127.0.0.1[:port]` or `localhost[:port]` while bound to loopback.
- Non-functional: library routes resolve every path against the configured
  output directory and reject anything escaping it.
- Non-functional: capture artifacts under `logs/` contain session cookies and are
  never served or listed by any route.
- Non-functional: the OpenAPI schema is dumpable to a file without starting the
  server, for offline type generation in phase 5.

## Related code files

- Create: `src/videotrack/server/__init__.py`, `server/app.py`,
  `server/settings.py`, `server/security.py`, `server/__main__.py`,
  `server/schema.py` (OpenAPI dump helper)
- Create: `src/videotrack/server/routes/resolve.py`, `routes/batch.py`,
  `routes/jobs.py`, `routes/library.py`, `routes/settings.py`,
  `routes/health.py`
- Modify: `pyproject.toml` (server extra, `filmdownloader-server` script),
  `requirements.txt`
- Create: `tests/test_api_resolve.py`, `tests/test_api_batch.py`,
  `tests/test_api_jobs.py`, `tests/test_api_library_paths.py`,
  `tests/test_api_security.py`

## Implementation steps

1. Add `fastapi` and `uvicorn[standard]` under
   `[project.optional-dependencies]` as a `server` extra so a CLI-only install
   stays light; `requirements.txt` installs the extra.
2. `server/settings.py`: settings from environment plus `data/settings.json`.
   State lives in `data/`, separate from the media output directory it names.
3. `server/app.py`: create the app, instantiate one `JobManager`, one
   `EventBus`, and one resolution cache on startup; call
   `recover_interrupted()`; shut the pool down cleanly. Mount routers under
   `/api`. Mount `server/static/` as the SPA at `/` with an index fallback for
   client-side routing, **only when that directory exists**, so the API runs
   before the frontend is built.
4. `server/security.py`: the token dependency (no-op on loopback, enforced
   otherwise, fail-closed when a wider bind has no token) plus the `Host`
   header guard from the requirements.
5. Routes as thin adapters over `engines.chain`, `engines.batch`, `JobManager`,
   and the store. Each body is a handful of lines.
6. Resolve and probe run in the threadpool under a timeout, with a resolve
   concurrency cap separate from the download cap, returning 429 when saturated.
   A chain or probe that finds nothing returns a structured 422 carrying the
   machine-readable reason, not a 500 with a stack trace.
7. SSE endpoint: async generator draining the subscriber queue with a comment
   heartbeat every 15 seconds so proxies and browsers hold the connection. No
   replay; the client reconciles against `GET /api/jobs`.
8. Library streaming: `FileResponse` for whole files, a manual ranged response
   when `Range` is present.
9. `server/schema.py`: write `app.openapi()` to `openapi.json` as a script entry
   point, so phase 5 and CI generate types from a file rather than from a
   running server.
10. Wire `core.preflight.check_tools()` and `shutil.disk_usage()` into
    `/api/health`.

## Validation

- `tests/test_api_resolve.py`: with a mocked chain, a success returns formats and
  a `resolution_id`; nothing found yields 422 with a reason, not a stack trace.
- `tests/test_api_batch.py`: a `proven` probe returns items; a `none` probe
  returns the reason verbatim; a `possible` probe is flagged so the UI can gate
  it; queueing 5 items creates 5 jobs under one `batch_id`; 2 failing leaves the
  other 3 `completed`.
- `tests/test_api_jobs.py`: queue, list, filter, cancel, retry against a
  temporary SQLite database and a fake executor.
- `tests/test_api_library_paths.py`: ids containing `..`, an absolute path, and a
  Windows drive prefix are each rejected with 404; a legitimate file is served,
  including a ranged request.
- `tests/test_api_security.py`: a non-loopback host with no token refuses to
  build the app; with a token, an unauthenticated `/api` call is 401; on
  loopback, a request carrying `Host: evil.example` is rejected.
- `python -m videotrack.server` starts, `/api/health` reports FFmpeg missing on
  this machine and free disk space, `/docs` renders.
- No route returns any path under `logs/`.

## Risk

SSE through `TestClient` can block a synchronous test. Mitigation: test the
event generator directly as an async unit and keep the route body trivial.

A long resolve or probe holds a threadpool worker. Mitigation: the separate
resolve cap plus 429.

## Rollback

The server package is standalone. Reverting removes `server/` and the extra
dependencies; the CLI and job layer are untouched.
