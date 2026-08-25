"""Short-lived cache of resolutions, keyed by an id the UI hands back.

The web flow is resolve, choose a format, then queue. Without this, queueing
would re-resolve: wasteful for yt-dlp, and for the browser engine a second full
Chrome session of 30-60 seconds per job.

The TTL is deliberately short. Captured media URLs frequently carry
time-limited tokens, so a long-lived cache trades one failure mode for another.
The worker's answer to an expired URL is to re-resolve once, on first-byte
failure, rather than to pay a second capture unconditionally.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass

from ..core.resolvers import Resolution

DEFAULT_TTL_SECONDS = 600.0
DEFAULT_MAX_ENTRIES = 256


@dataclass
class _Entry:
    resolution: Resolution
    expires_at: float


class ResolutionCache:
    def __init__(
        self,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock=time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, _Entry] = {}

    def put(self, resolution: Resolution) -> str:
        resolution_id = uuid.uuid4().hex
        with self._lock:
            self._evict_locked()
            self._entries[resolution_id] = _Entry(resolution, self._clock() + self.ttl_seconds)
        return resolution_id

    def get(self, resolution_id: str | None) -> Resolution | None:
        if not resolution_id:
            return None
        with self._lock:
            entry = self._entries.get(resolution_id)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                del self._entries[resolution_id]
                return None
            return entry.resolution

    def discard(self, resolution_id: str | None) -> None:
        if not resolution_id:
            return
        with self._lock:
            self._entries.pop(resolution_id, None)

    def _evict_locked(self) -> None:
        now = self._clock()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            del self._entries[key]
        while len(self._entries) >= self.max_entries:
            oldest = min(self._entries, key=lambda key: self._entries[key].expires_at)
            del self._entries[oldest]

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
