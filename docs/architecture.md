# Architecture

## Pipeline

```text
authorized page URL
  -> browser network capture
  -> media candidate detection and validation
  -> FFmpeg download
  -> MP4 plus optional metadata sidecar
```

## Modules

- `capture.py`: launches Chrome, records browser network requests, and retains the request context needed for permitted media access.
- `detect.py`: extracts, validates, and ranks media candidates from captured requests.
- `download.py`: downloads a selected candidate through FFmpeg and writes optional metadata.
- `crawl.py`: discovers in-scope URLs and writes a CSV queue.
- `cli.py`: composes the interactive and autonomous commands.
- `batch_run_csv.py`: processes a CSV queue sequentially and records resumable status.

## Local artifacts

`output/` holds downloaded media and batch state. `logs/` holds capture diagnostics and can contain session cookies. Both remain local and are excluded from version control.

## Extension points

- Add host-neutral candidate detectors in `detect.py`.
- Add supported-source adapters behind an explicit module boundary.
- Add small, synthetic test fixtures under `tests/`; do not commit captures, cookies, or media files.
- Add structured configuration through environment variables and an `.env.example` when credentials or runtime options are introduced.
