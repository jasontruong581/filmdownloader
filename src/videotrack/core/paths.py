"""Filesystem locations the tool writes to.

State and scratch space are deliberately separate from the media output
directory: the setting that names the output directory must not live inside the
directory it names.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_STATE = "FILMDOWNLOADER_STATE"
ENV_SCRATCH = "FILMDOWNLOADER_SCRATCH"
ENV_OUTPUT = "FILMDOWNLOADER_OUTPUT"


def _from_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name, "").strip()
    return Path(raw).expanduser() if raw else default


def state_dir() -> Path:
    """Durable local state: the job database and settings."""
    return _from_env(ENV_STATE, Path("data"))


def scratch_dir() -> Path:
    """Temporary working space for partial downloads and frame extraction."""
    return _from_env(ENV_SCRATCH, state_dir() / "tmp")


def output_dir() -> Path:
    """Default destination for finished media."""
    return _from_env(ENV_OUTPUT, Path("output"))


def ensure_scratch_dir() -> Path:
    path = scratch_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path
