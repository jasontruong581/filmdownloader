---
phase: 1
title: Adapter contracts
status: completed
priority: P1
effort: 2h
dependencies: []
---

# Phase 1: Adapter contracts

## Overview

Define a small, source-neutral result model and resolver protocol. Resolvers return media candidates plus the exact request context needed to download them; the existing browser flow remains unchanged when no resolver matches.

## Requirements

- Functional: represent a resolved stream URL, kind, referer, optional metadata, and resolver name.
- Functional: allow a resolver to decline unsupported pages without treating that as an error.
- Non-functional: keep the public data model independent from Selenium and source domains.

## Architecture

`Resolver` implementations receive a page URL and return `Resolution | None`. The CLI converts a successful result to the existing `StreamCandidate` and `CaptureResult` shapes, so download and validation code is reused.

## Related Code Files

- Create: `src/videotrack/resolvers.py`
- Modify: `src/videotrack/models.py`, `src/videotrack/cli.py`

## Implementation Steps

1. Add immutable resolver result models and the protocol.
2. Add helpers that translate a static resolution into current downloader inputs.
3. Add CLI selection for `auto`, `static`, and `browser` modes.

## Success Criteria

- [x] A resolver can resolve a stream without importing Selenium.
- [x] Existing browser commands retain their current behavior.
