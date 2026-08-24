"""Request and response models.

Every measurement is Optional for the same reason it is throughout: an extractor
that reports no duration or no total size must produce an honest null, not a zero
the UI would render as a stalled download.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ToolStatusOut(BaseModel):
    name: str
    required: bool
    available: bool
    path: str | None = None
    version: str | None = None


class HealthOut(BaseModel):
    ok: bool = Field(description="False when a required external tool is missing")
    tools: list[ToolStatusOut]
    ytdlp_version: str | None = None
    free_bytes: int | None = None
    output_dir: str
    plugins: list[str]


class FormatOut(BaseModel):
    format_id: str
    label: str
    track: str
    container: str | None = None
    height: int | None = None
    fps: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    filesize: int | None = None
    tbr: float | None = None


class ResolveIn(BaseModel):
    url: str
    engines: list[str] | None = None


class ResolveOut(BaseModel):
    resolution_id: str
    engine: str
    url: str
    final_url: str
    title: str
    duration: float | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    formats: list[FormatOut] = Field(default_factory=list)
    #: More than one when the URL enumerated several items.
    item_count: int = 1


class BatchItemOut(BaseModel):
    url: str
    title: str = ""


class BatchProbeIn(BaseModel):
    url: str


class BatchProbeOut(BaseModel):
    capability: str
    confidence: str
    batchable: bool
    items: list[BatchItemOut] = Field(default_factory=list)
    total_estimate: int | None = None
    truncated: bool = False
    #: Rendered verbatim by the UI when nothing enumerated.
    reason: str = ""


class BatchVerifyIn(BaseModel):
    items: list[BatchItemOut]
    count: int = 2


class BatchVerifyOut(BaseModel):
    verified: int
    attempted: int


class BatchQueueIn(BaseModel):
    items: list[BatchItemOut]
    source_url: str = ""
    capability: str = ""
    confidence: str = ""


class JobOut(BaseModel):
    id: str
    url: str
    batch_id: str | None = None
    engine: str = ""
    format_id: str | None = None
    title: str = ""
    output_path: str | None = None
    status: str
    phase: str
    percent: float | None = None
    downloaded_bytes: int | None = None
    total_bytes: int | None = None
    speed_bps: float | None = None
    eta_seconds: float | None = None
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""


class JobCreateIn(BaseModel):
    url: str | None = None
    resolution_id: str | None = None
    format_id: str | None = None
    title: str = ""


class BatchOut(BaseModel):
    id: str
    source_url: str = ""
    capability: str = ""
    confidence: str = ""
    created_at: str = ""
    jobs: list[JobOut] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)


class LibraryItemOut(BaseModel):
    id: str
    name: str
    size_bytes: int
    modified_at: float


class SettingsOut(BaseModel):
    output_dir: str
    concurrency: int
    engines: list[str]
    default_format: str
    ffmpeg_location: str
    cookies_from_browser: str
    host: str
    port: int


class SettingsIn(BaseModel):
    output_dir: str | None = None
    concurrency: int | None = None
    engines: list[str] | None = None
    default_format: str | None = None
    ffmpeg_location: str | None = None
    cookies_from_browser: str | None = None
