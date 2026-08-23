---
phase: 3
title: Collection workflow
status: completed
priority: P2
effort: 3h
dependencies:
  - 1
---

# Phase 3: Collection workflow

## Overview

Add a Flowplayer collection parser and a collection command that downloads direct media through the shared downloader while preserving a per-collection manifest and resumable results.

## Requirements

- Functional: parse HTML-encoded `data-item` JSON into ordered media entries.
- Functional: support a saved HTML page for tests and an authorized URL in use.
- Functional: write a manifest containing source, candidate, destination, and status.

## Related Code Files

- Create: `src/videotrack/collection.py`
- Modify: `src/videotrack/cli.py`

## Implementation Steps

1. Parse Flowplayer entries without adding a Node runtime dependency.
2. Add `collect` CLI command with URL/HTML inputs, dry-run, and output directory options.
3. Use direct HTTP/FFmpeg download based on source type and write a manifest after each item.

## Success Criteria

- [x] A static collection produces deterministic ordered entries.
- [x] Existing files are safely skipped unless overwrite is requested.
