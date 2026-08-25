"""Reading the documented `.env` file.

`.env.example` is the configuration surface the README tells operators to copy,
so an entry point has to read it. Parsed here with the standard library rather
than `python-dotenv`: that package is only ever present as a transitive
dependency of the server extra, and a CLI-only install would not have it.

Two properties matter more than parser completeness:

- **The real environment wins.** A shell override stays authoritative, and a
  stray file cannot reach into a test that pins a variable.
- **A malformed line is skipped, never fatal.** Refusing to start because a
  config file has a stray line would be a worse failure than ignoring it.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ENV_FILENAME = ".env"


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_env_text(text: str) -> dict[str, str]:
    """KEY=VALUE pairs from `.env` text, ignoring blanks, comments, and junk."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, separator, raw = stripped.partition("=")
        key = key.strip()
        if not separator or not key:
            continue
        values[key] = _unquote(raw.strip())
    return values


def load_env_file(path: Path | str | None = None) -> dict[str, str]:
    """Apply a `.env` file to `os.environ` and return only what it changed.

    Variables already set are left alone, so the return value is also the honest
    record of what the file actually contributed.
    """
    target = Path(path) if path is not None else Path(DEFAULT_ENV_FILENAME)
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    applied: dict[str, str] = {}
    for key, value in parse_env_text(text).items():
        if os.environ.get(key):
            continue
        os.environ[key] = value
        applied[key] = value
    return applied
