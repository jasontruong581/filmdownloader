# Architecture

## Layers

```text
cli.py                      server/
  argparse + console          FastAPI routes + SSE + static frontend
        \                    /
         \                  /
          jobs/  SQLite store, worker pool, event bus, resolution cache
                          |
         engines/  yt-dlp -> site plugins -> browser capture
                          |
          core/  neutral pipeline: detect, rank, download, executors
                          |
         sites/  everything site-specific, behind a registry
```

Two rules make the layering real, and both are enforced by tests:

- **`core` knows no site.** No hostnames, no site-specific selectors, no media
  conventions. It asks the `sites` registry instead.
- **`core` imports neither Selenium nor yt-dlp at module scope.** Browser
  capture and yt-dlp are optional; the CLI and the server must start without
  either installed.

## Resolution

```text
page URL
  -> engines.chain.resolve()
       1. yt-dlp          fast, no browser, widest coverage
       2. site plugins    pages yt-dlp has no extractor for
       3. browser capture works without recognizing anything
  -> list[Resolution]
```

One entry point returning a list; a single video is a list of one. There is no
scalar variant, so no caller has to pick between them.

An engine that does not recognize a page returns an empty list. An engine that
raises is logged and skipped: a broken engine never stops the chain.

`resolve_by_engine()` yields each engine's results lazily, which is what lets a
caller that also *downloads* fall through to the next engine when the download
fails rather than only when resolution fails.

## Download: two executors, one contract

| Executor | Handles | Why it exists |
|---|---|---|
| `core.ffmpeg_executor` | Candidates the browser or a site plugin found | Keeps the obfuscated-HLS repack, per-site asset conversion, header and cookie injection, and min-duration rejection |
| `engines.ytdlp_executor` | What yt-dlp resolved | Keeps yt-dlp's format merging, fragment retry, and postprocessing |

Funnelling everything through one would lose real capability in either
direction. The single cost of the split is that both must report through the same
vocabulary, which is `core.events`.

### The progress contract

Three facts the shape has to respect, learned from what the tools actually emit:

- **Percent can be unknown.** FFmpeg reports elapsed media time, which needs a
  probed duration that some HLS streams never provide. yt-dlp sometimes has
  neither a total nor an estimate. `percent` is optional, and a renderer needs an
  indeterminate state; faking zero would report a stall.
- **Totals mean different things.** FFmpeg's running total is bytes written so
  far, not the final size. Only `total_bytes` claims to be a size.
- **One download can be several files.** A yt-dlp split-format pick fetches
  video, then audio, then merges. `phase` distinguishes them and
  `MonotonicProgress` folds them into one non-decreasing track, instead of
  showing progress reach 100 percent twice and then stall.

## The pipeline

`core.pipeline` performs candidate collection, filtering, ranking, and download
with retry. It writes nothing to stdout and reads nothing from stdin; every
message is an event. That is the property that lets a server call it.

```text
capture -> detect candidates
        -> deep-scan embeds when nothing direct was found
        -> filter by allowed host, boost preferred hosts
        -> HLS precheck, ffprobe ranking
        -> attempt candidates in order until one yields an acceptable file
```

Interactive picking lives in the CLI as an injected reorder hook, so the
pipeline stays non-interactive by construction.

## Jobs

```text
queued -> resolving -> downloading -> postprocessing -> completed
                    \-> cancelled
                    \-> failed
   (process died)   \-> interrupted
```

- **Persistence** is stdlib `sqlite3` with WAL, one write lock, and a busy
  timeout, since the worker pool and the API thread both write.
- **Recovery**: a job left in an active status when the process died becomes
  `interrupted`. It is never reported as complete.
- **Concurrency** is gated by an admission count rather than the pool size,
  because a `ThreadPoolExecutor` cannot shrink and the setting must apply
  without a restart. Changing the limit never waits for a running job: lowering
  it lets the excess finish and stops admitting replacements. Waiting there
  deadlocked, since a worker needs the same lock to release its slot.
- **Output paths are claimed at submission**, against both the filesystem and
  the paths active jobs have already reserved. Existence alone is not enough:
  nothing is written yet at submit time, so a filesystem check hands one name to
  every job that derives it. An active duplicate of the same URL and format is
  refused.
- **Cancellation** is honest: FFmpeg and yt-dlp stop promptly, but a browser
  capture has no interruption hook, so cancellation lands between pipeline
  stages. The UI says "Cancelling" rather than pretending otherwise.

### The resolve-to-queue handoff

The UI resolves, the operator picks a format, then queues. Without a cache that
second step would re-resolve: a wasted extract for yt-dlp, and a second full
Chrome session of 30-60 seconds for browser capture. `jobs.cache` holds
resolutions under a short TTL, because captured media URLs often carry
time-limited tokens and caching them too long trades one failure for another.

## Batch capability

See [batch-capability.md](batch-capability.md). In short: three bounded
detectors, and the batch control unlocks only once concrete items have been
enumerated and shown.

## Site plugins

A plugin may contribute any of: a URL prefilter, a resolver, a metadata
extractor, a filename refinement, a sidecar writer, a postprocessor, crawl
rules, and a batch probe. All are optional.

`handles(url)` is a **cheap URL-only prefilter** and must not perform I/O.
Deciding whether a page's markup is recognized belongs inside
`resolver().resolve()`, which returns `None` when it is not: that check already
needs the page body, and doing it in `handles()` would cost one extra request per
plugin on every resolve.

Bundled plugins:

| Plugin | Claims | Contributes |
|---|---|---|
| `vlxx` | Its hostnames | Static player resolver, page metadata, description sidecar, crawl preset |
| `quatvn` | Its hostnames | Animated-WebP asset conversion, per-clip naming, crawl preset |
| `flowplayer` | Nothing by URL | Batch probing and collection parsing; its pattern is markup, not a hostname |

## Server

Routes are thin adapters under `/api`; no business logic lives in them. The built
frontend is served from `server/static/` when present, with an index fallback so
a hard refresh on a client route works. The fallback deliberately does not
shadow `/api`: an unknown API path answers with a JSON 404, not the app shell.

Resolution is capped separately from downloads and answers 429 when saturated,
since it is slow and holds a worker.

Configuration is layered, and the layers are read in one order: the `.env` file
loads first at the entry point, real environment variables always win over it,
and `settings.json` holds what the operator changed through the UI. The
FFmpeg location has a single resolver so the Settings field and the environment
variable cannot disagree about what was configured.

Security: loopback bind plus a `Host` check by default, token required for any
wider bind, and the server refuses to start on a wider bind without one. Library
paths are resolved against the output directory and required to stay under it.
Capture artifacts under `logs/` can contain session cookies and are never listed
or served.

## Local artifacts

| Directory | Holds | Tracked |
|---|---|---|
| `data/` | job database, settings | no |
| `output/` | downloaded media, collection manifests | no |
| `logs/` | capture diagnostics, may contain cookies | no |
| `src/videotrack/server/static/` | built frontend | no, build artifact |

## Extending

- **A new site**: add a module under `sites/`, subclass `BaseSitePlugin`,
  implement only the hooks it needs, and call `register()`. Nothing in `core`
  changes.
- **A new host-neutral detector**: add it to `core/detect.py`. Scoring data such
  as CDN preferences is passed in, not hardcoded there.
- **A new engine**: add it under `engines/` and to the chain's dispatch. Engine
  dispatch is late-bound on purpose, so an engine stays substitutable in tests.
- **A new batch detector**: add it to `engines/batch.py` and keep it bounded to a
  single request. Read `batch-capability.md` first.
- Keep test fixtures synthetic. Never commit captures, cookies, or media.
