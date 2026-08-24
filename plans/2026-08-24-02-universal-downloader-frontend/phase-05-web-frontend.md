---
phase: 5
title: React frontend
status: planned
priority: P1
effort: 12h
dependencies: [4]
order: 6
---

# Phase 5: React frontend

## Context

There is no frontend today. This phase adds a Vite + React + TypeScript SPA that
builds to static assets served by the FastAPI app, so running the tool is one
command and one browser tab. Node 24.18 and npm 11.16 are available on the dev
machine. The built bundle is a **gitignored build artifact**: a clean checkout
requires Node to produce it, which phase 6 documents.

## Requirements

- Functional: a URL bar that resolves and shows title, thumbnail, duration, and
  which engine matched, then lists selectable formats.
- Functional: **batch download, gated by capability.** See the tier table below.
- Functional: a queue view with live progress, speed, eta, and per-job cancel and
  retry, driven by SSE rather than polling, grouped by batch where applicable.
- Functional: a library view listing completed files, with inline playback
  against the ranged endpoint and a download action.
- Functional: a persistent banner when `/api/health` reports a missing tool or
  low free disk space, naming what to install.
- Functional: progress rendering must handle a **null percent** with an
  indeterminate bar, because some sources supply neither duration nor total
  bytes, and must show the phase label (`downloading:video`, `merging`) so a
  split-format download does not look stalled.
- Non-functional: TypeScript types generated from the backend OpenAPI schema, so
  a backend model change breaks the build rather than the runtime.
- Non-functional: the SPA works on a hard refresh at any client route, via the
  backend index fallback.
- Non-functional: no external CDN at runtime; the tool may run offline.
- Non-functional: `npm run build` outputs to `src/videotrack/server/static/`,
  which is gitignored.

## Batch UI, by tier

The batch control is never enabled by guessing from the URL. It is enabled by a
probe that produced a list the operator can see.

| Probe result | Control state | UI |
|---|---|---|
| Multi-line paste (tier 0) | **Always enabled** | Each line becomes an independent job immediately. No probe involved. |
| `proven` (playlist / collection) | Enabled | Checkbox list of the enumerated items with titles, select-all, count, and a truncation notice when capped. Optional "verify first 2" button. |
| `possible` (crawl preset) | Enabled behind confirmation | A distinct confirm dialog: these are page links, not proven media; a required max-items bound; explicit "I understand" before queueing. Never auto-queues. |
| `none` | Disabled | Renders the backend `reason` verbatim, e.g. "yt-dlp resolved a single video". Never a generic "not supported". |

Copy rule, enforced in review: no string in the UI may claim a site supports
batch download. The strongest claim available is "found N items", and after a
sample verify, "N items, first 2 resolve".

## Related code files

- Create: `web/package.json`, `web/vite.config.ts`, `web/tsconfig.json`,
  `web/index.html`, `web/tailwind.config.js`
- Create: `web/src/main.tsx`, `web/src/App.tsx`
- Create: `web/src/api/client.ts`, `web/src/api/types.gen.ts`,
  `web/src/api/useJobEvents.ts`
- Create: `web/src/views/ResolveView.tsx`, `views/BatchView.tsx`,
  `views/QueueView.tsx`, `views/LibraryView.tsx`, `views/SettingsView.tsx`
- Create: `web/src/components/FormatPicker.tsx`, `components/BatchItemList.tsx`,
  `components/CrawlConfirmDialog.tsx`, `components/JobRow.tsx`,
  `components/ProgressBar.tsx`, `components/HealthBanner.tsx`,
  `components/UrlInput.tsx`
- Modify: `.gitignore` (`web/node_modules/`, `web/dist/`,
  `src/videotrack/server/static/`), `README.md`

## Implementation steps

1. Scaffold `web/` with Vite, React, TypeScript, Tailwind. `build.outDir` set to
   `../src/videotrack/server/static`, `emptyOutDir: true`.
2. Dev proxy in `vite.config.ts` sending `/api` to `127.0.0.1:8756` so
   `npm run dev` gives hot reload against the real backend.
3. `npm run gen:api` generates `types.gen.ts` with `openapi-typescript` from the
   **`openapi.json` file** produced by the phase-4 dump script, not from a
   running server. Commit the generated file; phase 6 CI drift-checks it.
4. `api/client.ts`: typed fetch wrapper attaching the bearer token when
   configured, normalizing API error bodies into thrown errors that carry the
   machine-readable reason so views can render it.
5. `api/useJobEvents.ts`: an `EventSource` hook on `/api/jobs/events` reducing
   events into a job map, reconnecting with exponential backoff capped at 30
   seconds. Reconciles against `GET /api/jobs` on mount **and on every
   reconnect** — this is what makes the backend's replay buffer unnecessary.
6. `ResolveView`: URL input, resolve with a pending state, then `FormatPicker`
   grouping video, video-only, and audio-only, marking the recommended default
   and rendering the phase-2 label. Queue posts the `resolution_id` so the
   backend does not re-resolve.
7. `BatchView`: paste area for tier 0, a "check for batch" action calling
   `POST /api/batch/probe`, then the tier table above. Probe states are
   `idle`, `probing`, `proven`, `possible`, `none`, each with a distinct
   control state. Queue posts to `POST /api/batch/jobs`.
8. `QueueView`: rows grouped active and finished, batches collapsible with an
   n-of-m summary, `ProgressBar` supporting the indeterminate state, phase
   label, speed, eta, cancel showing "cancelling..." because cancellation is not
   instantaneous during a browser capture, and retry. Failed rows render the
   retained FFmpeg stderr from phase 3.
9. `LibraryView`: table from `/api/library`, inline `<video>` against the ranged
   endpoint, download link.
10. `SettingsView`: form over `/api/settings` with optimistic update and rollback
    on failure. Mark concurrency as live-applied and any restart-required field
    as such.
11. `HealthBanner`: renders on any missing tool or low disk space.
12. Add `python -m videotrack.server --open-browser` so one command starts the
    server and opens the UI.

## Validation

- `cd web && npm run build` produces assets in
  `src/videotrack/server/static/`, and `python -m videotrack.server` serves the
  UI at `http://127.0.0.1:8756/`.
- Manual end-to-end on an authorized URL: resolve, pick a format, queue, progress
  reaches 100 percent, file appears in the library and plays.
- Batch, all four tiers: a multi-line paste queues N jobs; a playlist URL
  enumerates and enables the control; a bare single-video URL leaves it disabled
  showing the backend reason; a crawl-preset host requires the confirm dialog.
- A yt-dlp split-format download shows one monotonic bar plus a `merging` phase,
  not two runs to 100 percent.
- A source with no duration shows an indeterminate bar, not a bar stuck at 0.
- Cancel mid-download: the row moves to cancelled and no partial file remains.
- Kill the server mid-download, restart, reload: the job shows interrupted and
  retry works.
- Hard refresh on `/queue` and `/library` loads the SPA, not a 404.
- Drop the SSE connection (stop and restart the server): the queue view
  reconciles to correct state without a manual reload.
- `npx tsc --noEmit` is clean.
- With the frontend not built, `python -m videotrack.server` still starts and
  `/api/health` responds, proving the static mount is optional.
- Copy review: no UI string claims a site supports batch download.

## Risk

Generated types drifting from the backend give false confidence. Mitigation: the
phase-6 CI drift check against the committed file.

The batch confirm dialog for tier 2 is the one place a mis-click can start a
large amount of work. Mitigation: the max-items bound is a required field with no
default high enough to be dangerous.

## Rollback

`web/` and the static mount are additive. Removing the built static directory
returns the project to an API-only tool.
