"""Job records.

Every measurement is optional for the same reason it is in the event
vocabulary: an extractor that reports no duration and no total size must produce
an honest "unknown", not a zero that reads as a stalled download.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..core.events import PHASE_DOWNLOADING, PipelineEvent


class JobStatus(str, Enum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    POSTPROCESSING = "postprocessing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: The process died while this job was running. Never reported as complete.
    INTERRUPTED = "interrupted"


#: Statuses that mean the job is not running and will not resume on its own.
TERMINAL_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED}
)

#: Statuses that mean a worker was mid-flight. Recovered as INTERRUPTED.
ACTIVE_STATUSES = frozenset(
    {JobStatus.QUEUED, JobStatus.RESOLVING, JobStatus.DOWNLOADING, JobStatus.POSTPROCESSING}
)


def new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Job:
    url: str
    id: str = field(default_factory=new_id)
    batch_id: str | None = None
    engine: str = ""
    #: Set when a prior resolve is being reused, so queueing does not re-resolve.
    resolution_id: str | None = None
    format_id: str | None = None
    title: str = ""
    output_path: str | None = None
    status: JobStatus = JobStatus.QUEUED
    phase: str = PHASE_DOWNLOADING
    percent: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed_bps: float | None = None
    eta_seconds: float | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "url": self.url,
            "batch_id": self.batch_id,
            "engine": self.engine,
            "resolution_id": self.resolution_id,
            "format_id": self.format_id,
            "title": self.title,
            "output_path": self.output_path,
            "status": self.status.value,
            "phase": self.phase,
            "percent": self.percent,
            "downloaded_bytes": self.downloaded_bytes,
            "total_bytes": self.total_bytes,
            "speed_bps": self.speed_bps,
            "eta_seconds": self.eta_seconds,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_row(row: dict[str, Any]) -> "Job":
        return Job(
            id=row["id"],
            url=row["url"],
            batch_id=row["batch_id"],
            engine=row["engine"] or "",
            resolution_id=row["resolution_id"],
            format_id=row["format_id"],
            title=row["title"] or "",
            output_path=row["output_path"],
            status=JobStatus(row["status"]),
            phase=row["phase"] or PHASE_DOWNLOADING,
            percent=row["percent"],
            downloaded_bytes=row["downloaded_bytes"],
            total_bytes=row["total_bytes"],
            speed_bps=row["speed_bps"],
            eta_seconds=row["eta_seconds"],
            error=row["error"],
            created_at=row["created_at"] or "",
            updated_at=row["updated_at"] or "",
        )


@dataclass(frozen=True)
class JobEvent:
    """A pipeline event attributed to a job."""

    job_id: str
    event: PipelineEvent
    created_at: str = ""
    batch_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "batch_id": self.batch_id,
            "created_at": self.created_at,
            **self.event.to_dict(),
        }


@dataclass
class Batch:
    id: str = field(default_factory=new_id)
    source_url: str = ""
    capability: str = ""
    confidence: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_url": self.source_url,
            "capability": self.capability,
            "confidence": self.confidence,
            "created_at": self.created_at,
        }
