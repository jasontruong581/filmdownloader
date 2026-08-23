---
title: Universal downloader adapter core
description: >-
  Create an extensible resolver layer so static source-specific discovery can
  run before browser-network fallback.
status: completed
priority: P1
branch: feat/universal-download-core
tags: []
blockedBy: []
blocks: []
created: '2026-08-23T17:27:51.348Z'
createdBy: 'ck:plan'
source: skill
---

# Universal downloader adapter core

## Overview

Turn the current browser-first downloader into a universal core that can use lightweight static resolvers when a known page structure exposes media directly. This PR does not add a frontend; it establishes the backend contract the frontend will call later.

## Phases

| Phase | Name | Status |
|-------|------|--------|
| 1 | [Adapter contracts](./phase-01-adapter-contracts.md) | Completed |
| 2 | [Static source resolvers](./phase-02-static-source-resolvers.md) | Completed |
| 3 | [Collection workflow](./phase-03-collection-workflow.md) | Completed |
| 4 | [Tests and documentation](./phase-04-tests-and-documentation.md) | Completed |

## Dependencies

- The browser capture pipeline remains the compatibility fallback.
- The frontend depends on this resolver contract and is intentionally out of scope for this PR.
