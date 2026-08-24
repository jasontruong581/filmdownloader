# FilmDownloader

A universal media downloader with a web UI and a CLI.

```text
paste URL -> resolve (yt-dlp -> site plugin -> browser capture)
          -> pick format -> queued job -> live progress -> file in library
```

## Responsible use

Use only with content you own or are authorized to access and download. This
project does not bypass DRM, paywalls, login controls, or other access
restrictions.

## Requirements

**Always required**

- Python 3.10+
- FFmpeg and ffprobe

FFmpeg is frequently not on `PATH` on Windows. That is expected rather than a
problem: set `FILMDOWNLOADER_FFMPEG` to its directory or executable, or set the
FFmpeg location in Settings. Run `python main.py doctor` to see exactly what was
found and where.

**Only for the browser fallback**

- Google Chrome, and a matching ChromeDriver if Selenium cannot fetch one

Needed only when yt-dlp and the site plugins both decline a page.

**Only for the quatvn plugin**

- ImageMagick (`magick`)

**Only to build the web UI**

- Node 20+ and npm

## Install

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e ".[server,dev]"
python main.py doctor
```

`doctor` is the first thing to run. It reports each external tool by resolved
path, the installed yt-dlp version, and the registered site plugins.

## Web UI

Build the frontend once, then run the server:

```bash
cd web
npm ci
npm run build
cd ..
python -m videotrack.server --open-browser
```

The UI is served at `http://127.0.0.1:8756/`. The API runs even when the
frontend has not been built, so `python -m videotrack.server` plus
`GET /api/health` works on a fresh checkout.

For frontend development, `npm run dev` proxies `/api` to a server you start
separately, giving hot reload against real data.

### Security posture

The server binds `127.0.0.1` by default and, on a loopback bind, rejects
requests whose `Host` header is not a loopback name. That second check matters:
loopback alone does not stop DNS rebinding, where a page points its own hostname
at `127.0.0.1` and then reaches this API as same-origin.

Binding anywhere else requires a token in `FILMDOWNLOADER_TOKEN`, and the server
refuses to start without one. The API resolves operator-supplied URLs
server-side and writes files to disk, so a reachable unauthenticated instance is
both an SSRF and a disk-write primitive.

## Batch download

The batch control is enabled by evidence, never by guessing from a URL. See
[docs/batch-capability.md](docs/batch-capability.md).

| What you give it | Confidence | What happens |
|---|---|---|
| Several URLs, one per line | n/a | Always allowed. Each resolves on its own. |
| A playlist or collection URL | `proven` | Items are enumerated and listed; the control unlocks. |
| A page whose host has crawl rules | `possible` | Explicit confirmation, with a deliberate item bound. |
| Nothing enumerable | `none` | Stays disabled, with the specific reason shown. |

A probe proves **enumeration, not downloadability**. Every item becomes an
independent job, so a failure affects only that item.

## CLI

```bash
# Resolve and download one page
python main.py run "https://example.org/video-page"

# See what formats an engine reports
python main.py list-formats "https://example.org/video-page"

# Download a specific format
python main.py run "https://example.org/video-page" --format 137

# Check whether a URL enumerates several items
python main.py probe-batch "https://example.org/playlist" --verify 2

# Persistent queue
python main.py queue add "https://example.org/a" "https://example.org/b" --wait
python main.py queue batch "https://example.org/playlist" --wait
python main.py queue list
python main.py queue retry <job-id>
python main.py queue recover

# Discover links, then queue them
python main.py crawl-links "https://example.org/" --output-csv output/links.csv
python main.py queue import-csv output/links.csv --dry-run
```

### Engine selection

`--engine` picks which engines to try, in order:

```bash
python main.py run <url> --engine ytdlp             # yt-dlp only
python main.py run <url> --engine ytdlp --engine site
python main.py run <url> --engine browser           # force browser capture
```

`--resolver auto|static|browser` is retained as a deprecated alias, so existing
scripts keep working.

### yt-dlp needs updating

Extractors go stale quickly. `doctor` prints the installed version; refresh it
with `pip install -U yt-dlp` when a site that used to work stops resolving.

### Candidate selection (browser and plugin paths)

- `--allow-host domain.example` limits candidates to an approved host.
- `--prefer-host domain.example` boosts a trusted host in ranking.
- `--interactive-pick` prompts before downloading.
- `--dump-all-candidates logs/candidates.json` saves everything detected.
- `--min-duration 120` rejects outputs shorter than N seconds.
- `--no-precheck-hls`, `--no-rank-with-ffprobe`, `--rank-top-n N` tune ranking.

### Collections

```bash
python main.py collect --url "https://example.org/collection/" --output-dir output/collections
```

Writes a resumable `manifest.json` per collection. `--dry-run` parses without
downloading; `--overwrite` re-downloads a file whose size already matches.

## Configuration

Copy `.env.example` and adjust. Every value is optional.

| Variable | Purpose |
|---|---|
| `FILMDOWNLOADER_OUTPUT` | Where finished media goes |
| `FILMDOWNLOADER_STATE` | Job database and settings (default `data/`) |
| `FILMDOWNLOADER_SCRATCH` | Temporary working files |
| `FILMDOWNLOADER_FFMPEG` | FFmpeg directory or executable, when not on PATH |
| `FILMDOWNLOADER_DB` | Job database path |
| `FILMDOWNLOADER_TOKEN` | API token, required for a non-loopback bind |
| `FILMDOWNLOADER_HOST` / `FILMDOWNLOADER_PORT` | Bind address |

State lives outside the media output directory on purpose: the setting that
names the output directory must not live inside the directory it names.

## Repository layout

```text
src/videotrack/
  core/       source-neutral pipeline: models, detect, download, capture,
              executors, pipeline, options, events, paths, preflight
  engines/    yt-dlp, browser capture, the resolver chain, batch probing
  sites/      plugin registry: vlxx static player, quatvn, flowplayer
  jobs/       SQLite store, worker pool, event bus, resolution cache
  server/     FastAPI app, routes, and the built frontend under static/
  cli.py      argparse layer over core.pipeline
web/          Vite + React + TypeScript source for the UI
data/         job database and settings (ignored)
output/       downloaded media (ignored)
logs/         capture diagnostics, may contain cookies (ignored)
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite runs with no network, no Chrome, no FFmpeg, no yt-dlp extraction, and
no built frontend.

## Architecture

See [docs/architecture.md](docs/architecture.md) for the resolver chain, the
two-executor split, and the job lifecycle. [docs/api.md](docs/api.md) documents
the HTTP surface.

### Moved import paths

The source-neutral modules now live under `videotrack.core`. If you have a local
script importing the old paths:

| Old | New |
|---|---|
| `videotrack.models` | `videotrack.core.models` |
| `videotrack.resolvers` | `videotrack.core.resolvers` |
| `videotrack.detect` | `videotrack.core.detect` |
| `videotrack.download` | `videotrack.core.download` |
| `videotrack.capture` | `videotrack.core.capture` |
| `videotrack.io` | `videotrack.core.io` |
| `videotrack.static_player` | `videotrack.sites.vlxx` |
| `videotrack.collection` | `videotrack.sites.flowplayer` |
| `videotrack.quatvn` | `videotrack.sites.quatvn` |

The `filmdownloader` console script and the `videotrack` package name are
unchanged.
