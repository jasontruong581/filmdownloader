---
phase: 1b
title: Pipeline extraction and event injection
status: planned
priority: P1
effort: 4h
dependencies: [1a, 2]
order: 3
---

# Phase 1b: Pipeline extraction and event injection

## Context

`cli.py` holds roughly 350 lines of pipeline orchestration that the job layer and
the API both need to call, with `print()` calls wired directly into the control
flow. Extracting it is the deepest internal surgery in this upgrade.

This phase runs **after** phase 2 on purpose: the event sink signature should be
designed knowing what yt-dlp's `progress_hooks` actually emit, so the pipeline
and the executors converge on one event shape rather than two that need adapting
later.

## Requirements

- Functional: identical behavior to the current `cmd_run`, `cmd_detect`, and
  `cmd_download` paths, including candidate ordering, retry count, and
  short-output rejection.
- Functional: every progress and diagnostic message becomes an event; the CLI
  subscribes with a console printer that reproduces today's output.
- Functional: one options object is shared by the CLI and the API, so a new knob
  cannot reach one caller and miss the other.
- Non-functional: `core/pipeline.py` performs no I/O to stdout directly.
- Non-functional: interactive prompting stays in `cli.py`; the pipeline is
  non-interactive by construction.

## Related code files

- Create: `src/videotrack/core/pipeline.py`, `core/options.py`,
  `core/events.py` (the event kinds shared with `jobs/`)
- Modify: `src/videotrack/cli.py` (becomes a thin argparse layer),
  `src/videotrack/core/download.py`, `batch_run_csv.py`
- Create: `tests/test_pipeline_events.py`

## Implementation steps

1. Define `PipelineOptions` in `core/options.py` as a dataclass covering
   `allow_hosts`, `prefer_hosts`, `precheck_hls`, `rank_with_ffprobe`,
   `rank_top_n`, `probe`, `min_duration`, `max_attempts`, `wait`, `extra_wait`,
   `headed`, `engines`, `format_selector`, `output_dir`, `ffmpeg_location`.
   This replaces the current eleven-positional-argument calls into
   `_prepare_candidates`.
2. Define the event vocabulary in `core/events.py`: a `PipelineEvent` with
   `kind`, `payload`, and a documented set of kinds
   (`stage_started`, `candidates_found`, `candidate_attempt`,
   `candidate_rejected`, `progress`, `download_completed`, `failed`). This is
   the same vocabulary phase 3 persists and phase 4 streams; defining it once
   here is what makes "the UI does not care which engine ran" true.
3. Move `_collect_candidates`, `_merge_candidates`, `_prepare_candidates`,
   `_rank_candidates_with_ffprobe`, `_probe_candidate_media`,
   `_boost_preferred_hosts`, `_resolve_download_capture`, and
   `_download_with_fallback` into `core/pipeline.py`, replacing each `print()`
   with `on_event(PipelineEvent(...))`. The sink defaults to a console printer
   that reproduces the current strings.
4. Leave `_interactive_reorder` in `cli.py`. It reads stdin; it is a terminal
   concern and must not be reachable from the API.
5. Rewrite `cli.py` command bodies as: parse args, build `PipelineOptions`,
   call the pipeline, return its exit code.
6. Point `batch_run_csv.py` at the pipeline directly instead of re-invoking
   `python main.py run` as a subprocess, which removes a process spawn per row
   and lets it consume events.

## Validation

- The phase-1a characterization tests still pass unchanged.
- `tests/test_pipeline_events.py`: a synthetic capture produces the expected
  event sequence for a successful run, for an all-candidates-failed run, and for
  a short-output rejection. This pins the contract phases 3 and 4 depend on.
- The step-1 `--help` capture from phase 1a still diffs clean.
- `python main.py run` console output on a real authorized URL is compared
  against the pre-extraction output; differences must be intentional.
- `grep -n "print(" src/videotrack/core/pipeline.py` returns nothing.

## Risk

The console printer drifting from today's strings would look like a regression to
the operator. Mitigation: build the printer by moving the exact format strings,
not by rewriting them.

## Rollback

One commit per step. Reverting the extraction restores `cli.py`'s previous body;
`core/` and `sites/` from phase 1a are unaffected.
