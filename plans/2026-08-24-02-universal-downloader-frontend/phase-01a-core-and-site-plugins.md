---
phase: 1a
title: Characterization tests, moves, plugin registry
status: planned
priority: P1
effort: 6h
dependencies: []
order: 1
---

# Phase 1a: Characterization tests, moves, plugin registry

## Context

The pipeline is already mostly source-neutral, but site-specific logic leaks
into core modules and blocks a universal tool. See known issues 1, 2, 3, 4, 5,
8, 10, 11, 12 in the plan index.

The dangerous part is not the moving; it is that **the existing 5 tests cover
almost none of the behavior being moved**. They exercise `resolvers.py`,
`static_player.py`, and `collection.py` only. Nothing covers `detect.py`
scoring and dedup, `cli.py:_merge_candidates`, `_download_with_fallback`
ordering and short-output rejection, output filename derivation, or
`build_ffmpeg_command` header and HLS flag construction. A `--help` diff pins
the argparse surface and nothing else. So characterization tests come first.

## Requirements

- Functional: moving code preserves every current CLI behavior and flag.
- Functional: a site plugin declares a cheap URL prefilter, an optional
  resolver, an optional metadata extractor, an optional postprocessor, an
  optional crawl preset, and an optional batch probe.
- Functional: core naming falls back to page title then URL path when no site
  plugin supplies metadata, and never collides silently.
- Non-functional: `videotrack.core.*` imports no Selenium, no site hostname, and
  no site-specific selector.
- Non-functional: `import videotrack.core.pipeline` **and**
  `import videotrack.sites` both succeed with Selenium absent.

## Related code files

- Create first: `tests/test_detect_scoring.py`, `tests/test_candidate_merge.py`,
  `tests/test_output_naming.py`, `tests/test_ffmpeg_command.py`
- Create: `src/videotrack/core/__init__.py`, `core/paths.py`,
  `core/preflight.py`
- Create: `src/videotrack/sites/__init__.py` (registry),
  `sites/vlxx.py`, `sites/quatvn.py`, `sites/flowplayer.py`
- Move: `models.py`, `resolvers.py`, `detect.py`, `download.py`, `capture.py`,
  `io.py` into `src/videotrack/core/`
- Move: `static_player.py` -> `sites/vlxx.py`;
  `quatvn.py` + the webp branch of `download.py` -> `sites/quatvn.py`;
  `collection.py` -> `sites/flowplayer.py`
- Modify: `src/videotrack/cli.py`, `src/videotrack/crawl.py`,
  `batch_run_csv.py`, `main.py`, `tests/*`

## Implementation steps

**Step 0 comes before any file is moved.**

0. Write characterization tests against current behavior, at current import
   paths, all offline:
   - `test_detect_scoring.py`: `detect_candidates(probe=False)` over a synthetic
     `CaptureResult` pins kind selection, score arithmetic, blocked-type
     rejection, ad penalty, host bonus, and dedup-keeps-higher-score.
   - `test_candidate_merge.py`: `_merge_candidates` pins source concatenation,
     score-wins field promotion, and final sort order.
   - `test_output_naming.py`: `output_path_for` and the
     `{page_id}-{video_code}` / quatvn `clip-NN` derivations.
   - `test_ffmpeg_command.py`: `build_ffmpeg_command` pins user-agent flag,
     the `Referer`/`Cookie` header block, and the HLS-only
     `-protocol_whitelist` / `-allowed_extensions` pair.
   These four files are the regression gate for every later step. The `--help`
   diff is a supplement to them, not the gate.
1. Capture `python main.py --help` plus each subcommand's `--help` to a file for
   later diffing.
2. Make Selenium lazy in **both** places: move the imports inside `_build_driver`
   / `capture_page` in `core/capture.py`, and inside
   `discover_quatvn_targets` / `_collect_tab_elements` in `sites/quatvn.py`.
   Raise a `RuntimeError` naming the missing dependency and how to install it.
3. Move the six neutral modules into `core/` with re-export shims at the old
   paths, so `batch_run_csv.py` and local scripts keep importing. Shims are
   deleted in phase 6.
4. Define the site plugin protocol in `sites/__init__.py`:

   ```python
   class SitePlugin(Protocol):
       name: str
       def handles(self, url: str) -> bool: ...          # cheap URL prefilter
       def resolver(self) -> Resolver | None: ...
       def claims_kind(self, kind: str) -> bool: ...      # postprocess routing
       def metadata(self, capture: CaptureResult) -> PageMetadata | None: ...
       def postprocess(self, capture, candidate, out_file) -> Path | None: ...
       def crawl_preset(self) -> CrawlPreset | None: ...
       def probe_batch(self, url: str) -> BatchProbe | None: ...   # phase 2
   ```

   Plus `register(plugin)`, `plugin_for(url)`, `plugin_for_kind(kind)`, and
   `iter_resolvers()`.

   `handles(url)` is a **URL-only prefilter** used for crawl presets, metadata,
   and postprocess routing. Hostnames are entirely acceptable here; confining
   them to `sites/` is the point of the directory. Markup sniffing must **not**
   move into `handles()` — it would cost one network GET per plugin per resolve,
   duplicating the fetch the resolver already performs. Family-generic markup
   detection stays inside `resolver().resolve()`, returning `None` on an
   unrecognized page, exactly as `StaticPlayerResolver` already does.
5. Move vlxx logic: `StaticPlayerResolver` and the `fetch_page_metadata`
   selectors go to `sites/vlxx.py`, along with the sidecar writer. Fix the
   `acctress:` key to `actress:`. Core `download.py` calls
   `plugin_for(url).metadata(...)` and falls back to page title then URL path.
   While here, pass the already-fetched page HTML into the metadata extractor
   where the caller has it, removing the duplicate GET (known issue 12).
6. Move quatvn logic: `quatvn.py` plus `_download_quatvn_stream_asset`,
   `_download_to_temp_file`, and `_quatvn_asset_suffix` go to `sites/quatvn.py`,
   reached through `postprocess()` routed by `claims_kind("quatvn_webp")`. Core
   `download_with_ffmpeg` loses its `quatvn_webp` branch entirely.
7. Add `core/paths.py` with one scratch-directory resolver (`scratch_dir()`,
   default `data/tmp`, overridable by `FILMDOWNLOADER_SCRATCH`) replacing the
   hardcoded `logs/` uses.
8. Fix output-name collisions in the neutral naming path: when the target exists
   or is claimed by another active job, append a numeric suffix rather than
   relying on `ffmpeg -y` to overwrite. Keep `-y` for the resume-overwrite case
   but stop it from being the collision policy.
9. Move the `tiktokcdn` / `bytecdn` scoring bonuses out of `core/detect.py`.
   They become either plugin-supplied scoring hints or a `prefer_hosts` default
   in settings. Same for `batch_run_csv.py`'s `--prefer-host tiktokcdn.com` and
   `output/vlxx_links.csv` defaults, which become neutral.
10. Turn `crawl.py` presets into plugin-supplied presets: `resolve_crawl_preset`
    asks the registry first, keeping `generic` as the built-in default, and
    `--site-preset` choices become dynamic from registered plugin names.
11. Add `core/preflight.py` with `check_tools()` reporting presence, version, and
    **resolved path** of `ffmpeg`, `ffprobe`, `chromedriver`, and `magick`. It
    honors a configured `ffmpeg_location` rather than only searching PATH,
    because FFmpeg is not on PATH on the primary dev machine. Expose it as a
    `doctor` subcommand. Also make the FFmpeg runner catch `FileNotFoundError`
    and report "ffmpeg not found at <path>" instead of letting it escape
    (known issue 10).

## Validation

- The four step-0 characterization test files pass before the move and, byte-for
  byte unchanged except for import paths, after it.
- `PYTHONPATH=src python -m unittest discover -s tests` passes with Selenium
  **not** installed, fixing the current `test_resolvers.py` import error.
- `python -c "import videotrack.core.pipeline"` and
  `python -c "import videotrack.sites"` both succeed with Selenium absent.
- `grep -rniE 'vlxx|quatvn|xvideo|flowplayer|video-code|actress-tag|tiktokcdn|bytecdn' src/videotrack/core/`
  returns nothing.
- The `--help` capture from step 1 diffs clean against the post-refactor output.
- `python main.py doctor` reports FFmpeg missing on this machine rather than
  failing at download time, and reports it by resolved path.
- Two synthetic candidates with identical derived titles produce two distinct
  output paths.

## Risk

Wide file moves can silently drop behavior. Mitigation: step 0 first, then one
commit per step, running the suite after each.

The plugin protocol is being designed before its phase-2 consumer exists, so
`probe_batch` may need reshaping. Mitigation: it is declared optional and
returns `None` by default; phase 2 owns its semantics.

## Rollback

Each step is one commit on `feat/universal-engine-and-web-ui`. Revert the
offending commit; the shims mean earlier steps stay importable.
