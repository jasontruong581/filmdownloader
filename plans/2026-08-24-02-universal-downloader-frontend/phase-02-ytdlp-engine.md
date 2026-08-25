---
phase: 2
title: yt-dlp engine, executors, batch probe
status: planned
priority: P1
effort: 7h
dependencies: [1a]
order: 2
---

# Phase 2: yt-dlp engine, executors, batch probe

## Context

`StreamCandidate` describes a guessed URL with a heuristic score. yt-dlp returns
a structured format list: resolution, fps, codecs, filesize, tbr, plus
audio-only and video-only variants. A UI that lets the operator pick "1080p mp4"
needs that structure, so the model grows before the API exists.

yt-dlp is not installed in the current environment and must be added.

This phase also introduces the **executor split**, which is the accepted design:
yt-dlp downloads what yt-dlp resolved, FFmpeg downloads everything else. That
keeps the obfuscated-HLS PNG-tail repack, the quatvn webp path, the tuned header
and cookie injection, and min-duration rejection, none of which yt-dlp would
preserve. The single cost of the split is a normalized progress contract, paid
once, here.

## Requirements

- Functional: a `MediaFormat` model carries format_id, container, resolution,
  fps, vcodec, acodec, filesize (exact or estimated), tbr, and a display label.
- Functional: `Resolution` carries `formats`, `engine`, and enough metadata
  (title, uploader, duration, thumbnail) that filenames no longer depend on
  scraping.
- Functional: the resolver chain runs yt-dlp, then site plugins, then browser
  capture; each link declines by returning empty rather than raising.
- Functional: one chain entry point returning a list. A single video is a list of
  one. No scalar variant exists for callers to choose wrongly.
- Functional: a `DownloadExecutor` contract with two implementations, so the job
  layer selects an executor without knowing engine internals.
- Functional: a `BatchProbe` capability check that enumerates items cheaply and
  reports a confidence level, never a hostname allowlist verdict.
- Non-functional: `core/` must not import yt-dlp. The yt-dlp executor lives in
  `engines/`, and executor selection happens in the pipeline and job layers.
- Non-functional: all tests in this phase run offline against fixture dicts.

## Related code files

- Create: `src/videotrack/engines/__init__.py`, `engines/chain.py`,
  `engines/ytdlp_resolver.py`, `engines/ytdlp_executor.py`,
  `engines/browser_resolver.py`, `engines/batch.py`
- Create: `src/videotrack/core/executor.py` (the contract + ffmpeg executor
  adapter over the existing `download.py`)
- Modify: `core/models.py` (`MediaFormat`), `core/resolvers.py` (`Resolution`),
  `sites/flowplayer.py`, `sites/quatvn.py` (implement `probe_batch`),
  `cli.py`, `pyproject.toml`, `requirements.txt`
- Create: `tests/test_ytdlp_resolver.py`, `tests/test_resolver_chain.py`,
  `tests/test_batch_probe.py`, `tests/test_executor_events.py`

## Implementation steps

1. Add yt-dlp to dependencies with a **current** floor. A 2024 floor is
   meaningless in 2026; extractor freshness is the entire value. `doctor` prints
   the installed version with an update hint, which is more effective than a
   README paragraph alone.
2. Add `MediaFormat` to `core/models.py` with a `label()` producing the UI
   string, e.g. `1080p / mp4 / 2.4 Mbps / 412 MB`. Filesize and tbr are
   **nullable**; many extractors supply neither.
3. Extend `Resolution` with `formats`, `engine`, `duration`, `thumbnail`,
   `uploader`.
4. Define the executor contract in `core/executor.py`:

   ```python
   class DownloadExecutor(Protocol):
       name: str
       def run(self, request: DownloadRequest, cancel: threading.Event,
               on_event: Callable[[PipelineEvent], None]) -> Path: ...
   ```

   `FfmpegExecutor` wraps the existing `download.py` path. `YtDlpExecutor` lives
   in `engines/` so `core/` never imports yt-dlp.
5. **Normalize the progress event across both executors.** This is the crux;
   without it the phase-3 claim that the UI is engine-agnostic is false. Write
   the mapping table into this file when implementing, and honor these three
   facts:
   - **Percent is nullable.** FFmpeg gives `out_time_us`, which needs an ffprobe
     duration that some HLS streams do not provide. yt-dlp sometimes has neither
     `total_bytes` nor `total_bytes_estimate` on fragmented HLS. The UI needs an
     indeterminate state, so the model must permit absence rather than faking 0.
   - **Totals are nullable and mean different things.** FFmpeg's `total_size` is
     bytes-written-so-far, not final size.
   - **A download can be multi-file.** A yt-dlp DASH pick fetches video-only then
     audio-only then merges. Naive hook mapping shows progress reach 100 percent,
     reset to 0, reach 100 percent again, then stall during merge. Carry a
     `phase` field (`downloading:video`, `downloading:audio`, `merging`,
     `postprocessing`) and aggregate weighted progress so the reported track is
     monotonic.
6. Implement `YtDlpResolver`:
   - `extract_info(url, download=False)` under `quiet=True`, `no_warnings=True`,
     a `socket_timeout`, and a wall-clock guard.
   - Return empty on `UnsupportedError`, `DownloadError`, timeout, or an empty
     `formats` list. Declining is never an exception at the call site.
   - Map `info["formats"]` through a single `_map_format()` using `.get()`
     defaults, so a yt-dlp release changing a field name degrades the label
     instead of raising.
   - A playlist yields one `Resolution` per entry.
7. Implement `engines/chain.py` with one entry:

   ```python
   def resolve(url, *, options) -> list[Resolution]
   ```

   Engines are tried in `options.engines` order; the first non-empty result
   wins; a raising engine is caught and the chain continues. `--engine` selects
   a subset on the CLI, with `--resolver` retained as a documented alias.
8. Add `engines/browser_resolver.py` wrapping `capture_page` in the `Resolver`
   protocol so the chain treats all three engines uniformly.
9. **Batch probe** in `engines/batch.py`:

   ```python
   @dataclass(frozen=True)
   class BatchProbe:
       capability: str      # "playlist" | "collection" | "crawl" | "none"
       confidence: str      # "proven" | "possible" | "none"
       items: tuple[BatchItem, ...]
       total_estimate: int | None
       truncated: bool
       reason: str          # rendered verbatim by the UI when nothing was found
   ```

   `probe(url, options) -> BatchProbe` runs bounded detectors in order:
   - **yt-dlp flat**: `extract_info(extract_flat="in_playlist", playlistend=N)`.
     `_type == "playlist"` with >= 2 entries gives `capability="playlist"`,
     `confidence="proven"`. Flat extraction deliberately skips per-entry format
     resolution, so this costs a fraction of a real resolve.
   - **Site plugin** `probe_batch(url)`: flowplayer counts `data-item` entries,
     reusing the existing `parse_flowplayer_collection`; quatvn uses
     `discover_quatvn_targets`. One GET. `confidence="proven"`.
   - **Crawl prefilter**: fetch **exactly one** page and count links matching the
     host's crawl preset. >= 2 gives `capability="crawl"`,
     `confidence="possible"` — page links, not proven media. The full crawler is
     never invoked by a probe.
   - Nothing matched: `confidence="none"` with a specific `reason`
     ("yt-dlp resolved a single video", "no plugin registered for this host",
     "generic crawl preset found no child links").

   Probe results are cached by URL with a TTL, sharing the resolution cache
   introduced in phase 3.

   The probe proves **enumeration, not downloadability**. Nothing in the return
   value or its documentation may be phrased as "this site supports batch".
10. Add an optional `sample_verify(items, n=2)` helper that fully resolves the
    first n items, so the UI can raise confidence from "found N items" to
    "N items, first 2 resolve" without paying N resolves.
11. Unify collections with playlists: the flowplayer plugin's resolver returns
    multiple resolutions through the same list-returning chain. Collections then
    get the phase-5 batch UI for free, with no second code path, and `collect`
    survives as a CLI alias.
12. Teach the executor selection to honor `ffmpeg_location` in both executors
    (yt-dlp accepts it directly; the command builder takes a path). FFmpeg stays
    required in both paths, for HLS remux and for yt-dlp's merge step.
13. Add an off-by-default `cookies_from_browser` setting mapped to yt-dlp's
    `cookiesfrombrowser`. The browser-capture path already forwards session
    cookies, so without this the yt-dlp path is strictly worse for
    authorized-but-cookied content. Passthrough only. Document honestly that
    Chrome's app-bound cookie encryption can make it fail on current Chrome, so
    it is best-effort. This does not change the responsible-use boundary: it
    reuses a session the operator already holds and bypasses no access control.
14. Cancellation for the yt-dlp path: check the cancel `Event` inside the
    progress hook and raise `DownloadCancelled`, then clean up yt-dlp's own
    `.part` files.
15. Add `--format`, `--list-formats`, and `--probe-batch` CLI flags so the engine
    and the probe are exercisable from the terminal before any UI exists.

## Validation

- `tests/test_ytdlp_resolver.py`: fixture `info` dicts, no network. Asserts sort
  order, label rendering, nullable filesize handling, and that a missing field
  degrades rather than raises.
- `tests/test_resolver_chain.py`: all engines mocked. yt-dlp success
  short-circuits; empty falls to the site plugin; both empty falls to browser
  capture; a raising engine is caught and the chain continues.
- `tests/test_batch_probe.py`: a playlist fixture yields `proven`; a
  single-video fixture yields `none` with a reason; a flowplayer HTML fixture
  yields `proven` with the right count; a crawl fixture yields `possible`; the
  probe never invokes the multi-page crawler (assert with a spy).
- `tests/test_executor_events.py`: a fake yt-dlp split-format hook sequence
  (video to 100, audio to 100, merge) produces **one monotonic** progress track;
  a source with no duration and no total bytes produces `percent is None`
  rather than 0.
- `python main.py run <authorized yt-dlp URL> --engine ytdlp` downloads without
  Chrome starting.
- `python main.py run <vlxx-family URL> --engine site` still uses the phase-1a
  plugin.
- `python main.py --probe-batch <playlist URL>` prints the enumerated items.
- `grep -rn "yt_dlp" src/videotrack/core/` returns nothing.

## Risk

`extract_info` can hang on some hosts. Mitigation: `socket_timeout` plus a
wall-clock guard, surfacing a timeout as a decline so the chain moves on.

yt-dlp's return shape changes between releases. Mitigation: one `_map_format()`
with `.get()` defaults.

The batch probe could become a de facto crawler if the crawl detector is allowed
to page. Mitigation: the one-page bound is a hard requirement with a spy test.

## Rollback

The chain and the probe are additive. Reverting means dropping `engines/` and
restoring the phase-1a `cmd_run` body; nothing in `core/` depends on yt-dlp.
