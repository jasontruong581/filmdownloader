"""One options object shared by every caller of the pipeline.

The pipeline used to be reached through functions taking eleven positional
arguments, which made it easy to add a knob that reached the CLI and missed the
API. Building this object is now the only way in, so a new field is either
visible everywhere or nowhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PipelineOptions:
    """Everything that steers candidate selection and download."""

    # Where finished media goes.
    output_dir: Path = Path("output")

    # Candidate discovery.
    probe: bool = True
    wait: int = 15
    extra_wait: int = 45
    headed: bool = False

    # Candidate filtering and ranking.
    allow_hosts: list[str] = field(default_factory=list)
    prefer_hosts: list[str] = field(default_factory=list)
    host_bonuses: tuple[tuple[str, int], ...] = ()
    precheck_hls: bool = True
    rank_with_ffprobe: bool = True
    rank_top_n: int = 6

    # Download attempts.
    pick: int = 1
    max_attempts: int = 5
    min_duration: int = 120
    format_id: str | None = None
    ffmpeg_location: str | None = None

    @classmethod
    def from_args(cls, args, host_bonuses: tuple[tuple[str, int], ...] = ()) -> "PipelineOptions":
        """Build from an argparse namespace, tolerating absent flags.

        Subcommands expose different subsets of the flags, so every read falls
        back to this class's default rather than requiring the attribute.
        """
        defaults = cls()

        def value(name: str, default):
            return getattr(args, name, default)

        return cls(
            output_dir=Path(value("output_dir", defaults.output_dir)),
            probe=not value("no_probe", False),
            wait=value("wait", defaults.wait),
            extra_wait=value("extra_wait", defaults.extra_wait),
            headed=value("headed", defaults.headed),
            allow_hosts=list(value("allow_host", []) or []),
            prefer_hosts=list(value("prefer_host", []) or []),
            host_bonuses=host_bonuses,
            precheck_hls=not value("no_precheck_hls", False),
            rank_with_ffprobe=not value("no_rank_with_ffprobe", False),
            rank_top_n=value("rank_top_n", defaults.rank_top_n),
            pick=value("pick", defaults.pick),
            max_attempts=value("max_attempts", defaults.max_attempts),
            min_duration=value("min_duration", defaults.min_duration),
            format_id=value("format_id", "") or None,
            ffmpeg_location=value("ffmpeg_location", None),
        )
