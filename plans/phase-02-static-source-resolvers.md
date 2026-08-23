---
phase: 2
title: Static source resolvers
status: completed
priority: P1
effort: 3h
dependencies:
  - 1
---

# Phase 2: Static source resolvers

## Overview

Port the reusable static discovery ideas from the WordPress-player project: parse page metadata, invoke the declared player endpoint, and extract direct media or embed/API URLs before using browser automation.

## Requirements

- Functional: resolve direct HLS, DASH, and progressive media from known static player markup.
- Functional: preserve referer and static metadata for the shared downloader.
- Non-functional: do not embed a specific site hostname or credentials.

## Related Code Files

- Create: `src/videotrack/static_player.py`
- Modify: `src/videotrack/cli.py`

## Implementation Steps

1. Fetch an authorized page with a normal user agent.
2. Parse declared player identifiers and route to the relative player endpoint.
3. Extract direct, JW-style, and iframe candidates; fall back cleanly when unsupported.
4. Register the resolver ahead of browser capture in auto mode.

## Success Criteria

- [x] Static markup can yield a candidate without launching Chrome.
- [x] Unsupported or changed markup falls back to browser mode.
