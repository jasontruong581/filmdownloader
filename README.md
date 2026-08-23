# FilmDownloader

Python CLI for an authorized media workflow:

```text
analyze browser traffic -> detect media candidates -> download with FFmpeg
```

## Responsible use

Use only with content you own or are authorized to access and download. This project does not bypass DRM, paywalls, login controls, or other access restrictions.

## Requirements

- Python 3.10+
- Google Chrome and a compatible ChromeDriver available on `PATH`
- FFmpeg and ffprobe available on `PATH`

## Install

```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
```

## Quick start

Run the complete pipeline for an authorized page:

```bash
python main.py run "https://example.org/video-page" --wait 20 --extra-wait 60 --headed
```

Run stages separately when diagnosing a page:

```bash
python main.py analyze "https://example.org/video-page" --wait 20 --capture-out logs/capture.json --headed
python main.py detect --capture logs/capture.json --dump-all-candidates logs/candidates_all.json
python main.py download --capture logs/capture.json --pick 1 --output-dir output
```

`capture.json` can contain cookies and request headers. It is intentionally ignored by Git; treat it as sensitive local diagnostic data.

## Crawl and batch workflow

Create a CSV of matching links from one host:

```bash
python main.py crawl-links "https://example.org/" --output-csv output/links.csv --max-pages 500
```

Optional crawl filters:

- `--include-substring "/video/"` keeps matching paths.
- `--exclude-substring "/tag/"` skips matching paths; repeat as needed.
- `--site-preset generic` disables a host-specific preset.

Process pending CSV rows sequentially:

```bash
python batch_run_csv.py --csv output/links.csv --batch-size 10 --start-order 1
```

The batch runner maintains `order`, `id`, `proceed_status`, and error fields in the CSV, so interrupted work can resume.

## Candidate selection

- `--dump-all-candidates logs/candidates_all.json` saves candidates detected on the page and embeds.
- `--interactive-pick` lets you choose a candidate before downloading.
- `--allow-host domain.example` limits candidates to an approved host.
- `--prefer-host domain.example` boosts a trusted host in ranking.
- `--no-precheck-hls` disables HLS segment validation.
- `--no-rank-with-ffprobe` disables duration/bitrate ranking.
- `--rank-top-n 10` changes how many candidates receive ffprobe ranking.
- `--min-duration 120` rejects outputs shorter than the given duration in seconds.

## Repository layout

```text
src/videotrack/       Core capture, detection, crawl, and download modules
main.py               CLI entry point
batch_run_csv.py      Resumable sequential CSV runner
docs/                 Project planning material
output/               Local media and batch state (ignored)
logs/                 Local captures and diagnostics (ignored)
```
