"""Server settings.

Read from the environment, overlaid with a JSON file in the state directory.
State is deliberately not stored under the media output directory, because this
file is what names that directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..core.paths import output_dir as default_output_dir
from ..core.paths import state_dir
from ..core.preflight import ENV_FFMPEG
from ..engines.chain import DEFAULT_ENGINE_ORDER
from ..jobs.manager import DEFAULT_CONCURRENCY

ENV_HOST = "FILMDOWNLOADER_HOST"
ENV_PORT = "FILMDOWNLOADER_PORT"
ENV_TOKEN = "FILMDOWNLOADER_TOKEN"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8756

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def settings_path() -> Path:
    return state_dir() / "settings.json"


@dataclass
class Settings:
    output_dir: str = ""
    concurrency: int = DEFAULT_CONCURRENCY
    engines: list[str] = field(default_factory=lambda: list(DEFAULT_ENGINE_ORDER))
    #: Preferred format when the operator does not pick one explicitly.
    default_format: str = ""
    ffmpeg_location: str = ""
    #: Off by default. Best effort: current Chrome's app-bound cookie encryption
    #: can defeat it. Reuses a session the operator already holds and bypasses
    #: no access control.
    cookies_from_browser: str = ""
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @property
    def is_loopback(self) -> bool:
        return self.host in LOOPBACK_HOSTS

    @property
    def resolved_output_dir(self) -> Path:
        return Path(self.output_dir) if self.output_dir else default_output_dir()

    @property
    def resolved_ffmpeg_location(self) -> Path | None:
        """Configured FFmpeg directory or executable, if one was set.

        `load_settings` already folds the environment variable in, so this is the
        single place that answers the question. Reading the variable directly
        instead would ignore whatever the operator set in Settings.
        """
        raw = self.ffmpeg_location.strip()
        return Path(raw).expanduser() if raw else None

    def to_dict(self) -> dict:
        data = asdict(self)
        data["output_dir"] = str(self.resolved_output_dir)
        return data


#: Fields an operator may change through the API. Bind host and port are not
#: among them: changing where the server listens is not a runtime operation.
EDITABLE_FIELDS = (
    "output_dir",
    "concurrency",
    "engines",
    "default_format",
    "ffmpeg_location",
    "cookies_from_browser",
)


def _from_file() -> dict:
    path = settings_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def load_settings() -> Settings:
    settings = Settings(**{key: value for key, value in _from_file().items() if key in Settings().__dict__})

    # The environment wins: it is how a launcher pins host, port, and paths.
    if os.environ.get(ENV_HOST, "").strip():
        settings.host = os.environ[ENV_HOST].strip()
    settings.port = _int_env(ENV_PORT, settings.port)
    if os.environ.get(ENV_FFMPEG, "").strip():
        settings.ffmpeg_location = os.environ[ENV_FFMPEG].strip()
    return settings


def save_settings(settings: Settings, path: Path | str | None = None) -> Settings:
    """Persist settings, by default to the state directory.

    The destination is a parameter for the same reason the job database path is:
    a test that exercises the settings endpoint must not rewrite the operator's
    real configuration file.
    """
    path = Path(path) if path is not None else settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    return settings


def token() -> str:
    return os.environ.get(ENV_TOKEN, "").strip()
