---
title: Universal downloader engine and web frontend
description: >-
  Make the downloader site-neutral with yt-dlp as the primary resolver, move
  site-specific logic behind a plugin registry, add a persistent job layer, and
  expose a FastAPI + React frontend with capability-gated batch download.
status: completed
priority: P1
branch: feat/universal-engine-and-web-ui
tags: []
blockedBy: []
blocks: []
created: '2026-08-24'
revised: '2026-08-24'
createdBy: 'manual'
source: session
---

# Universal downloader engine and web frontend

## Outcome

One tool that downloads from any supported site through a browser UI:

```text
paste URL -> resolve (yt-dlp -> site plugin -> browser capture)
          -> pick format -> queued job -> live progress -> file in library

paste URL -> batch probe -> enumerated item list -> select -> N independent jobs
```

The CLI keeps working. Site-specific code stops leaking into the core.

## Accepted decisions

| Decision | Choice |
|----------|--------|
| Resolve engine | yt-dlp primary, then site plugins, then Chrome network capture |
| Frontend | FastAPI backend + React/Vite SPA served as static assets |
| Existing vlxx/quatvn code | Moved behind a `sites/` plugin registry, core becomes site-neutral |
| Batch download | Gated on a bounded probe that enumerates real items, never on a hostname allowlist |
| Download execution | Two executors (FFmpeg, yt-dlp) behind one normalized progress event contract |

## Batch download capability model

The frontend must offer batch download, but the control is only enabled once the
tool can **prove** it enumerated more than one item. Certainty comes from showing
the operator the actual list, not from a hardcoded list of "batchable" sites.

| Tier | Trigger | Confidence | Gate |
|------|---------|-----------|------|
| 0 | Operator pastes N URLs, one per line | n/a | **Always enabled.** Each URL resolves independently; nothing is inferred about any site. |
| 1 | One URL that enumerates: yt-dlp playlist/channel, or a site plugin collection | `proven` | Enabled when the probe returns >= 2 concrete items, shown as a checkbox list before queueing. |
| 2 | One URL whose host has a crawl preset that finds child links | `possible` | Enabled only behind an explicit confirmation carrying a max-pages bound and a warning that these are page links, not proven media. |
| - | Nothing enumerated | `none` | Disabled, with the specific reason rendered. |

Honest contract, enforced in the API shape and the UI copy: **a probe proves
enumeration, not downloadability.** Therefore:

- Every enumerated item becomes an independent job. Failures are per item; a
  batch is never all-or-nothing.
- No response and no UI string claims "this site supports batch download". The
  claim is only ever "found N items".
- An optional **sample verify** resolves the first 1-2 items before queueing the
  rest, which is the strongest confidence obtainable without paying N resolves.

Every probe is bounded: yt-dlp uses flat extraction with a `playlistend` cap, a
plugin probe is a single GET, and the tier-2 probe fetches exactly **one** page
and counts matching links rather than running the crawler.

## Non-goals

- Renaming the `videotrack` package or the `filmdownloader` console script. The
  import path and entry point stay as public contracts.
- Bypassing DRM, paywalls, or login controls. Unchanged from the current stance.
- Multi-user accounts or auth. The server is a single-operator local tool.
- Replacing FFmpeg with a Python muxer.
- A full crawler UI. Tier-2 batch exposes a single-page link probe; running the
  multi-page crawl stays a CLI command (`crawl-links`). Decided, not omitted.
- Funnelling browser-capture downloads through yt-dlp. Two executors is the
  accepted design; see the progress contract below.

## Constraints

- Python 3.10+ (`pyproject.toml` floor); dev machine currently runs 3.12.10.
- FFmpeg and ffprobe stay external binaries. **They are not on PATH in the
  current dev environment**, which is the normal Windows state rather than an
  anomaly, so a configurable `ffmpeg_location` and a preflight check are
  requirements, not conveniences.
- Offline unit tests must not need Chrome, FFmpeg, yt-dlp network access, or a
  built frontend bundle.
- The server binds `127.0.0.1` by default. Any wider bind is opt-in and requires
  a token, because the API resolves operator-supplied URLs server-side and
  writes files to disk.
- Windows is the primary platform. Process termination and file-locking
  semantics there govern the cancellation design.

## Phases

Sequence is deliberate: the mechanical move lands first, then the riskiest
**external** integration (yt-dlp), and only then the deepest **internal**
surgery (`cli.py` extraction), so the event contract is designed knowing what
yt-dlp's hooks actually emit.

| Order | Phase | Name | Status | Depends on |
|-------|-------|------|--------|-----------|
| 1 | 1a | [Characterization tests, moves, plugin registry](./phase-01a-core-and-site-plugins.md) | **Done** | - |
| 2 | 2 | [yt-dlp engine, executors, batch probe](./phase-02-ytdlp-engine.md) | **Done** | 1a |
| 3 | 1b | [Pipeline extraction and event injection](./phase-01b-pipeline-extraction.md) | **Done** | 1a, 2 |
| 4 | 3 | [Job store and progress events](./phase-03-job-layer.md) | **Done** | 1b, 2 |
| 5 | 4 | [FastAPI backend](./phase-04-api-server.md) | **Done** | 3 |
| 6 | 5 | [React frontend](./phase-05-web-frontend.md) | **Done** | 4 |
| 7 | 6 | [Tests, docs, packaging](./phase-06-tests-docs-packaging.md) | **Done** | all |

## Effort

| Phase | Estimate | Note |
|-------|---------|------|
| 1a | 6h | includes ~2.5h characterization tests written before any move |
| 2 | 7h | playlist expansion, executor contract, batch probe, cancellation |
| 1b | 4h | pipeline extraction, event injection, `PipelineOptions`, `doctor` |
| 3 | 9h | hardest phase: FFmpeg progress rewrite, Windows cancel, resolution cache |
| 4 | 7h | SSE/thread bridging, Host guard, batch probe route |
| 5 | 12h | five views, SSE reconciliation, batch tier UI, typegen |
| 6 | 6h | clean-checkout validation on two platforms |
| **Total** | **~51h** | |

## Target layout

```text
src/videotrack/
  core/       models, resolver contract, detect, download (ffmpeg executor),
              capture, pipeline, paths, preflight
  engines/    ytdlp_resolver.py (+ its executor), browser_resolver.py, chain.py
  sites/      registry + vlxx static player, quatvn, flowplayer collection
  jobs/       job models, SQLite store, worker manager, progress events
  server/     FastAPI app, routes, built frontend under server/static/
  cli.py      thin argparse layer over core.pipeline
data/         jobs.db and settings.json (state, separate from media output)
web/          Vite + React + TypeScript source for the SPA
```

## Known issues this upgrade must fix

Each verified against the current code and assigned to a phase.

1. `download.py:fetch_page_metadata` scrapes vlxx-only selectors
   (`video-code`, `actress-tag`, `page-title`) on **every** download and names
   output files from them. Universal downloads get wrong or empty names. 1a.
2. `download.py` special-cases `candidate.kind == "quatvn_webp"` and shells out
   to ImageMagick from core download code. 1a.
3. `cli.py` imports `capture.py`, which imports Selenium at module top level, so
   `tests/test_resolvers.py` cannot import without Selenium installed. 1a.
4. `quatvn.py:9-11` **separately** imports Selenium at module top level, so
   importing the site registry would drag Selenium in even after issue 3 is
   fixed. The server must be able to import the registry without Chrome. 1a.
5. `detect.py:49-50` hardcodes `tiktokcdn.com` and `bytecdn` scoring bonuses in
   what becomes `core/detect.py`; `batch_run_csv.py:33` defaults
   `--prefer-host tiktokcdn.com` and `:27` defaults the CSV to
   `output/vlxx_links.csv`. The core-neutrality grep must cover these. 1a.
6. `build_ffmpeg_command` passes `-y` and names come from title or URL, so two
   items with the same title silently overwrite each other, and phase-3
   concurrency makes concurrent same-name writes possible. 1a and 3.
7. `batch_run_csv.py:98` skips work by globbing `{video_id}-*.mp4`, so the
   output naming scheme is load-bearing for batch resume. Changing naming
   without migrating this check silently re-downloads or silently skips. 3.
8. `_download_to_temp_file` and `_download_quatvn_stream_asset` hardcode `logs/`
   as their scratch directory. 1a.
9. FFmpeg runs with `capture_output=False`, so progress only reaches the
   terminal and the error is reported as a bare "ffmpeg failed" with the stderr
   discarded. No parseable progress exists for a UI. 3.
10. A missing FFmpeg raises `FileNotFoundError` from `subprocess.run` rather
    than a nonzero return code, so the current error path does not catch it. 1a.
11. The description sidecar writes the misspelled key `acctress:`. 1a.
12. `fetch_page_metadata` re-fetches page HTML the browser already retrieved,
    costing a duplicate GET per download. 1a, opportunistic.
13. `batch_run_csv.py` keeps queue state in a CSV. The job store supersedes it;
    CSV import is retained. 3.

## Acceptance criteria

- [x] `python main.py run <url>` still resolves and downloads with the same
      flags it accepts today.
- [x] A yt-dlp-supported URL resolves without launching Chrome.
- [x] A URL that yt-dlp rejects still falls through to a site plugin and then
      to browser capture.
- [x] `core/`, `engines/`, `jobs/`, and `server/` contain no site hostnames and
      no site-specific selectors; `import videotrack.sites` succeeds with
      Selenium absent.
- [x] The web UI resolves a URL, lists selectable formats, queues a download,
      shows live progress with a working indeterminate state, and cancels.
- [x] Batch: pasting multiple URLs always works; a playlist or collection URL
      enumerates its items and enables the batch control; a bare single-video
      URL leaves it disabled with a stated reason; a crawl-preset host enables
      it only after explicit confirmation.
- [x] A yt-dlp split-format download (separate video and audio) reports one
      monotonic progress track, not two runs to 100 percent.
- [x] Jobs survive a server restart with their status intact; an interrupted job
      is never reported as complete.
- [x] Cancelling leaves no partial file in the output directory.
- [x] Two queued items with identical titles produce two distinct files.
- [x] `python -m unittest discover -s tests` passes with no Chrome, no FFmpeg,
      no network, and no built frontend bundle.
- [x] `README.md` and `docs/architecture.md` describe the resolver chain, the
      executor split, the batch tiers, the server, and the frontend build.

## Advisory review

Reviewed by the `advisor` agent on 2026-08-24 against the full source tree. Six
must-fix design gaps and thirteen should-change items were folded into the
phases above; the effort estimate rose from 34h to ~51h. Endorsed and left
unchanged: the two-executor design, SSE over WebSocket, threads plus
`run_in_threadpool`, stdlib `sqlite3` with WAL, loopback-default with
fail-closed token, `.part`-then-rename, shims retained until phase 6, and the
3 -> 4 -> 5 ordering.
