"""The objects a running server shares across requests.

Held in one place so routes stay thin adapters and a test can build the whole
thing against a temporary database.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

from ..core.options import PipelineOptions
from ..core.paths import state_dir
from ..engines.chain import ChainOptions
from ..engines.ytdlp_resolver import YtDlpOptions
from ..hosts import DEFAULT_HOST_BONUSES
from ..jobs.bus import EventBus
from ..jobs.cache import ResolutionCache
from ..jobs.manager import JobManager
from ..jobs.store import JobStore
from .settings import Settings, save_settings

#: Resolution is slow and holds a worker, so it is capped separately from
#: downloads and saturation answers 429 rather than queueing indefinitely.
DEFAULT_RESOLVE_SLOTS = 2


@dataclass
class ServerState:
    settings: Settings
    store: JobStore
    bus: EventBus
    cache: ResolutionCache
    manager: JobManager
    resolve_slots: threading.Semaphore
    #: Where `apply_settings` writes. None means the state directory.
    settings_file: Path | None = None

    @classmethod
    def build(
        cls,
        settings: Settings,
        db_path: Path | str | None = None,
        resolve_slots: int = DEFAULT_RESOLVE_SLOTS,
        settings_path: Path | str | None = None,
    ) -> "ServerState":
        store = JobStore(db_path if db_path is not None else state_dir() / "jobs.db")
        bus = EventBus()
        cache = ResolutionCache()
        manager = JobManager(
            store=store,
            bus=bus,
            cache=cache,
            options=cls.pipeline_options(settings),
            concurrency=settings.concurrency,
        )
        return cls(
            settings=settings,
            store=store,
            bus=bus,
            cache=cache,
            manager=manager,
            resolve_slots=threading.Semaphore(resolve_slots),
            settings_file=Path(settings_path) if settings_path is not None else None,
        )

    @staticmethod
    def pipeline_options(settings: Settings) -> PipelineOptions:
        return PipelineOptions(
            output_dir=settings.resolved_output_dir,
            host_bonuses=DEFAULT_HOST_BONUSES,
            ffmpeg_location=settings.ffmpeg_location or None,
        )

    def chain_options(self, engines: list[str] | None = None) -> ChainOptions:
        order = tuple(engines or self.settings.engines)
        return ChainOptions(
            engines=order,
            ytdlp=YtDlpOptions(cookies_from_browser=self.settings.cookies_from_browser or None),
        )

    def apply_settings(self, settings: Settings) -> Settings:
        self.settings = save_settings(settings, self.settings_file)
        self.manager.options = self.pipeline_options(settings)
        self.manager.set_concurrency(settings.concurrency)
        return self.settings

    def free_bytes(self) -> int | None:
        target = self.settings.resolved_output_dir
        probe = target if target.exists() else target.parent
        try:
            return shutil.disk_usage(probe).free
        except OSError:
            return None

    def shutdown(self) -> None:
        self.manager.shutdown()
        self.store.close()
