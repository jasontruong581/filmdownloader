# Architecture

## Pipeline

```text
authorized page URL
  -> resolver selection
       -> static-player resolver (supported page metadata + player endpoint)
       -> browser network capture (fallback or --resolver browser)
  -> normalized capture context and media candidates
  -> candidate detection, validation, and ranking
  -> FFmpeg download
  -> MP4 plus optional metadata sidecar

authorized Flowplayer collection URL
  -> parse HTML-encoded data-item entries
  -> per-collection manifest
  -> FFmpeg download for each direct media entry
```

## Modules

- `resolvers.py`: defines the source-neutral `Resolver` contract and converts a static resolution into the capture-shaped context used by the shared pipeline.
- `static_player.py`: resolves the currently supported static player pattern: page metadata, a player endpoint, then media URLs or an embedded player/API response.
- `capture.py`: launches Chrome, records browser network requests, and retains the request context needed for permitted media access.
- `detect.py`: extracts, validates, and ranks media candidates from capture-shaped request data, regardless of whether it originated from a resolver or Chrome.
- `download.py`: downloads a selected candidate through FFmpeg and writes optional metadata.
- `collection.py`: parses static Flowplayer collection entries, writes a resumable manifest, validates existing direct downloads by content length, and sends missing items through the shared downloader.
- `crawl.py`: discovers in-scope URLs and writes a CSV queue.
- `cli.py`: composes the interactive and autonomous commands.
- `batch_run_csv.py`: processes a CSV queue sequentially and records resumable status.

## Local artifacts

`output/` holds downloaded media, CSV batch state, and `collection-slug/manifest.json` files. `logs/` holds capture diagnostics and can contain session cookies. Both remain local and are excluded from version control.

## Extension points

- Add host-neutral candidate detectors in `detect.py`.
- Add supported-source adapters behind the `Resolver` module boundary, emitting `Resolution`/`ResolvedMedia` rather than directly invoking FFmpeg.
- Keep resolver order explicit: static adapters first, browser-network capture as the compatibility fallback; adapters must return `None` when their page structure is not recognized.
- Keep collection adapters separate from single-page resolvers when one page intentionally describes multiple downloads.
- Add small, synthetic test fixtures under `tests/`; do not commit captures, cookies, or media files.
- Add structured configuration through environment variables and an `.env.example` when credentials or runtime options are introduced.
