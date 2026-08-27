"""External tool discovery.

FFmpeg frequently is not on PATH on Windows, which is the normal state rather
than an anomaly, so a configured location is a first-class option and the check
reports the path it actually resolved.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ENV_FFMPEG = "FILMDOWNLOADER_FFMPEG"

#: How to decode an external tool's output.
#:
#: `text=True` alone decodes with the locale codec, which on a Windows console
#: is an ANSI codepage. FFmpeg writes bytes that are not valid there - a stream
#: title, a codec tag, a URL from a foreign-language page - and the decoder
#: raises rather than substituting. That killed the thread reading FFmpeg
#: stderr outright, so a failed download arrived with no diagnosis at all: the
#: reader had died before the reason was printed.
#:
#: `errors="replace"` is deliberate. A mangled character in a diagnostic is a
#: cosmetic problem; an exception in a reader thread destroys the diagnostic.
TEXT_OUTPUT: dict[str, object] = {"text": True, "encoding": "utf-8", "errors": "replace"}

#: Tool name -> whether the pipeline can run at all without it.
TOOLS: tuple[tuple[str, bool], ...] = (
    ("ffmpeg", True),
    ("ffprobe", True),
    ("chromedriver", False),
    ("magick", False),
)

_VERSION_FLAG = {"magick": "--version"}


@dataclass(frozen=True)
class ToolStatus:
    name: str
    required: bool
    path: str | None
    version: str | None

    @property
    def available(self) -> bool:
        return self.path is not None

    @property
    def blocking(self) -> bool:
        return self.required and not self.available


def ffmpeg_location() -> Path | None:
    """Configured directory or executable for the FFmpeg tools, if any."""
    raw = os.environ.get(ENV_FFMPEG, "").strip()
    return Path(raw).expanduser() if raw else None


def resolve_tool(name: str, location: Path | None = None) -> str | None:
    """Absolute path to a tool, honoring the configured location before PATH."""
    location = location if location is not None else ffmpeg_location()
    if location is not None and name in {"ffmpeg", "ffprobe"}:
        if location.is_dir():
            found = shutil.which(name, path=str(location))
            if found:
                return found
        elif location.exists() and location.stem.lower() == name:
            return str(location)
    return shutil.which(name)


def _tool_version(path: str, name: str) -> str | None:
    try:
        result = subprocess.run(
            [path, _VERSION_FLAG.get(name, "-version")],
            capture_output=True,
            **TEXT_OUTPUT,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0].strip() if output else None


def check_tools(location: Path | None = None) -> tuple[ToolStatus, ...]:
    statuses = []
    for name, required in TOOLS:
        path = resolve_tool(name, location)
        statuses.append(
            ToolStatus(
                name=name,
                required=required,
                path=path,
                version=_tool_version(path, name) if path else None,
            )
        )
    return tuple(statuses)


def format_report(statuses: tuple[ToolStatus, ...]) -> str:
    lines = []
    for status in statuses:
        mark = "ok  " if status.available else ("MISS" if status.required else "--  ")
        tail = f"{status.path}" if status.available else ("required" if status.required else "optional")
        lines.append(f"[{mark}] {status.name:12} {tail}")
        if status.version:
            lines.append(f"            {status.version}")
    return "\n".join(lines)
