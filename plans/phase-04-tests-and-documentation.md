---
phase: 4
title: Tests and documentation
status: completed
priority: P1
effort: 2h
dependencies:
  - 1
  - 2
  - 3
---

# Phase 4: Tests and documentation

## Overview

Lock the new resolver contract and static parsers behind synthetic tests, document the resolver order, and keep all fixture data free of cookies, real media URLs, and copyrighted assets.

## Related Code Files

- Create: `tests/test_static_player.py`, `tests/test_collection.py`
- Modify: `README.md`, `docs/architecture.md`, `requirements.txt`

## Implementation Steps

1. Add synthetic HTML/JSON tests for resolver and collection parsing.
2. Run the Python test suite and compile checks.
3. Document resolver order and the intended frontend boundary.

## Success Criteria

- [x] Tests run without network, Chrome, or FFmpeg.
- [x] Documentation describes the auto-resolution order and limits.
