"""Logging setup for the entry points.

The package logs progress through `logging` rather than printing, because the
pipeline has to stay usable from a server where stdout is not a progress
channel. Nothing configured a handler for it, so those records went nowhere and
a long browser capture looked identical to a hang.

Only the `videotrack` logger is configured. Touching the root logger would
fight whatever the host application - uvicorn, a test runner, an embedding
script - has already set up.
"""

from __future__ import annotations

import logging
import sys

ENV_LOG_LEVEL = "FILMDOWNLOADER_LOG_LEVEL"

DEFAULT_LEVEL = "INFO"

#: A timestamp is the point: the reader is watching a slow operation and needs to
#: see that something moved, not only what it was.
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%H:%M:%S"

_HANDLER_NAME = "videotrack-console"

#: Spelled out rather than read from `logging.getLevelNamesMapping()`, which
#: only exists from 3.11 while this package supports 3.10.
LEVEL_NAMES = frozenset({"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"})


def resolve_level(explicit: str | None = None) -> str:
    """The requested level: an explicit choice, else the environment, else INFO."""
    for candidate in (explicit, _from_env()):
        if not candidate:
            continue
        name = candidate.strip().upper()
        if name in LEVEL_NAMES:
            return name
    return DEFAULT_LEVEL


def _from_env() -> str:
    import os

    return os.environ.get(ENV_LOG_LEVEL, "")


def configure_logging(level: str | None = None) -> str:
    """Send `videotrack` records to stderr at the resolved level.

    Idempotent: calling it twice replaces the handler rather than doubling every
    line. Writes to stderr so a command whose stdout is data - a dumped schema,
    a piped listing - stays clean.
    """
    resolved = resolve_level(level)
    logger = logging.getLogger("videotrack")

    for existing in list(logger.handlers):
        if getattr(existing, "name", None) == _HANDLER_NAME:
            logger.removeHandler(existing)

    handler = logging.StreamHandler(sys.stderr)
    handler.name = _HANDLER_NAME
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=TIME_FORMAT))
    logger.addHandler(handler)
    logger.setLevel(resolved)
    #: The host application decides what else to do with these records; without
    #: this a root handler would print every line a second time.
    logger.propagate = False
    return resolved
