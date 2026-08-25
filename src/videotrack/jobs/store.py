"""SQLite-backed job persistence.

Stdlib `sqlite3`, no ORM. The worker pool and the API thread both write, so the
connection allows cross-thread use and every write goes through one lock with
WAL journalling and a busy timeout, which is what keeps "database is locked" from
surfacing under concurrency.

State lives in the state directory, deliberately not in the media output
directory: the setting that names the output directory must not live inside the
directory it names.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from ..core.paths import state_dir
from .models import ACTIVE_STATUSES, Batch, Job, JobStatus

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    source_url TEXT NOT NULL DEFAULT '',
    capability TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    batch_id TEXT,
    engine TEXT NOT NULL DEFAULT '',
    resolution_id TEXT,
    format_id TEXT,
    title TEXT NOT NULL DEFAULT '',
    output_path TEXT,
    status TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'downloading',
    percent REAL,
    downloaded_bytes INTEGER,
    total_bytes INTEGER,
    speed_bps REAL,
    eta_seconds REAL,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status);
CREATE INDEX IF NOT EXISTS jobs_batch_idx ON jobs (batch_id);
CREATE INDEX IF NOT EXISTS jobs_created_idx ON jobs (created_at);
"""

_JOB_COLUMNS = (
    "id",
    "url",
    "batch_id",
    "engine",
    "resolution_id",
    "format_id",
    "title",
    "output_path",
    "status",
    "phase",
    "percent",
    "downloaded_bytes",
    "total_bytes",
    "speed_bps",
    "eta_seconds",
    "error",
    "created_at",
    "updated_at",
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db_path() -> Path:
    return state_dir() / "jobs.db"


class JobStore:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else default_db_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    # --- schema --------------------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                self._conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
            self._conn.commit()

    @property
    def schema_version(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
        return int(row["version"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- jobs ----------------------------------------------------------------

    def add(self, job: Job) -> Job:
        now = utcnow()
        job.created_at = job.created_at or now
        job.updated_at = now
        columns = ", ".join(_JOB_COLUMNS)
        placeholders = ", ".join("?" for _ in _JOB_COLUMNS)
        data = job.to_dict()
        values = [data[name] for name in _JOB_COLUMNS]
        with self._lock:
            self._conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({placeholders})", values)
            self._conn.commit()
        return job

    def update(self, job: Job) -> Job:
        job.updated_at = utcnow()
        assignments = ", ".join(f"{name} = ?" for name in _JOB_COLUMNS if name != "id")
        data = job.to_dict()
        values = [data[name] for name in _JOB_COLUMNS if name != "id"]
        values.append(job.id)
        with self._lock:
            self._conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)
            self._conn.commit()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(dict(row)) if row else None

    def list(
        self,
        status: JobStatus | str | None = None,
        batch_id: str | None = None,
        limit: int | None = None,
    ) -> list[Job]:
        query = "SELECT * FROM jobs"
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value if isinstance(status, JobStatus) else status)
        if batch_id is not None:
            clauses.append("batch_id = ?")
            params.append(batch_id)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at ASC, rowid ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [Job.from_row(dict(row)) for row in rows]

    def active_for(self, url: str, format_id: str | None) -> Job | None:
        """An unfinished job for the same URL and format, if one exists.

        Guards against two workers racing on the same output path, and against
        an operator queueing the same thing twice by accident.
        """
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        params: list[object] = [url]
        params.extend(sorted(s.value for s in ACTIVE_STATUSES))
        query = f"SELECT * FROM jobs WHERE url = ? AND status IN ({placeholders})"
        if format_id is None:
            query += " AND format_id IS NULL"
        else:
            query += " AND format_id = ?"
            params.append(format_id)
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
        return Job.from_row(dict(row)) if row else None

    def claimed_output_paths(self) -> set[str]:
        """Output paths reserved by jobs that have not written their file yet.

        Active jobs only. A finished job's file is on disk, where an existence
        check already sees it, and a cancelled job's name is free to reuse.
        """
        statuses = sorted(status.value for status in ACTIVE_STATUSES)
        placeholders = ", ".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                "SELECT output_path FROM jobs "
                f"WHERE output_path IS NOT NULL AND status IN ({placeholders})",
                statuses,
            ).fetchall()
        return {row["output_path"] for row in rows if row["output_path"]}

    def recover_interrupted(self) -> list[Job]:
        """Mark jobs that were mid-flight when the process died.

        A job left in an active status is not complete and must never be reported
        as such, so it becomes INTERRUPTED and is resumable.
        """
        placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
        params = sorted(s.value for s in ACTIVE_STATUSES)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders})", params
            ).fetchall()
            if rows:
                self._conn.execute(
                    f"UPDATE jobs SET status = ?, updated_at = ? WHERE status IN ({placeholders})",
                    [JobStatus.INTERRUPTED.value, utcnow(), *params],
                )
                self._conn.commit()
        recovered = []
        for row in rows:
            job = Job.from_row(dict(row))
            job.status = JobStatus.INTERRUPTED
            recovered.append(job)
        return recovered

    def counts_by_status(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status").fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    # --- batches -------------------------------------------------------------

    def add_batch(self, batch: Batch) -> Batch:
        batch.created_at = batch.created_at or utcnow()
        with self._lock:
            self._conn.execute(
                "INSERT INTO batches (id, source_url, capability, confidence, created_at) VALUES (?, ?, ?, ?, ?)",
                (batch.id, batch.source_url, batch.capability, batch.confidence, batch.created_at),
            )
            self._conn.commit()
        return batch

    def get_batch(self, batch_id: str) -> Batch | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
        if row is None:
            return None
        return Batch(
            id=row["id"],
            source_url=row["source_url"],
            capability=row["capability"],
            confidence=row["confidence"],
            created_at=row["created_at"],
        )
