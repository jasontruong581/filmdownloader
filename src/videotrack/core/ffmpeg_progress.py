"""Parse FFmpeg's `-progress` stream into normalized progress samples.

FFmpeg writes repeating `key=value` blocks terminated by a `progress=` line. The
useful fields are `out_time_us` (elapsed media time), `total_size` (bytes written
so far, *not* the final size), and `speed`.

Percent needs a known duration. Some HLS streams do not report one, so percent
stays None and a renderer shows an indeterminate bar rather than a bar stuck at
zero.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import PHASE_DOWNLOADING, Progress

#: Lines FFmpeg emits that carry values we use.
_NUMERIC_KEYS = ("out_time_us", "out_time_ms", "total_size")


def _to_int(value: str) -> int | None:
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return None


def _to_float(value: str) -> float | None:
    try:
        return float(value.strip().rstrip("x"))
    except (TypeError, ValueError):
        return None


@dataclass
class FfmpegProgressParser:
    """Accumulates `key=value` lines and yields a sample per completed block."""

    duration_seconds: float | None = None
    phase: str = PHASE_DOWNLOADING
    _fields: dict[str, str] = field(default_factory=dict)
    _last_total_size: int | None = None

    def feed(self, line: str) -> Progress | None:
        """Consume one line. Returns a sample when a block completes."""
        line = line.strip()
        if not line or "=" not in line:
            return None

        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        self._fields[key] = value

        if key != "progress":
            return None

        sample = self._build_sample(finished=value == "end")
        self._fields.clear()
        return sample

    def _elapsed_seconds(self) -> float | None:
        raw_us = self._fields.get("out_time_us")
        if raw_us is not None:
            microseconds = _to_int(raw_us)
            if microseconds is not None and microseconds >= 0:
                return microseconds / 1_000_000
        raw_ms = self._fields.get("out_time_ms")
        if raw_ms is not None:
            milliseconds = _to_int(raw_ms)
            # FFmpeg has historically written microseconds under out_time_ms;
            # treating it as milliseconds would overstate progress 1000x, so it
            # is only a fallback and is clamped by the duration below.
            if milliseconds is not None and milliseconds >= 0:
                return milliseconds / 1_000_000
        return None

    def _build_sample(self, finished: bool) -> Progress:
        elapsed = self._elapsed_seconds()

        percent: float | None = None
        if finished:
            percent = 100.0
        elif elapsed is not None and self.duration_seconds and self.duration_seconds > 0:
            percent = min(100.0, max(0.0, elapsed / self.duration_seconds * 100.0))

        total_size = _to_int(self._fields.get("total_size", "")) if "total_size" in self._fields else None
        if total_size is not None:
            self._last_total_size = total_size

        speed_bps: float | None = None
        speed_multiplier = _to_float(self._fields.get("speed", ""))
        if speed_multiplier is not None and speed_multiplier > 0 and elapsed and elapsed > 0:
            written = self._last_total_size
            if written:
                speed_bps = written / (elapsed / speed_multiplier)

        eta: float | None = None
        if percent is not None and 0 < percent < 100 and elapsed and self.duration_seconds:
            remaining_media = max(self.duration_seconds - elapsed, 0.0)
            if speed_multiplier and speed_multiplier > 0:
                eta = remaining_media / speed_multiplier

        return Progress(
            phase=self.phase,
            percent=round(percent, 2) if percent is not None else None,
            # FFmpeg reports bytes written so far; the final size is unknown, so
            # total_bytes is deliberately left unset.
            downloaded_bytes=self._last_total_size,
            total_bytes=None,
            speed_bps=speed_bps,
            eta_seconds=eta,
        )
