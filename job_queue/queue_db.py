"""Shared SQLite queue: a cross-job index of metadata paths and their
last-known SLURM status, so you can `SELECT * FROM jobs` for a dashboard
without opening every per-job JSON file individually.

This is a secondary index, not the source of truth for any one job -
the per-job metadata file (metadata.py) still owns that.
"""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from .config import QUEUE_DB_PATH, ensure_queue_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    job_name        TEXT NOT NULL,
    metadata_path   TEXT NOT NULL UNIQUE,
    slurm_job_id    INTEGER,
    status          TEXT NOT NULL DEFAULT 'NEW',
    submitted_at    TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_slurm_id  ON jobs(slurm_job_id);
"""


class QueueDB:
    def __init__(self, db_path: Optional[Path] = None):
        ensure_queue_dir()
        self.db_path = Path(db_path) if db_path else QUEUE_DB_PATH
        self._init_schema()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")  # let readers/writers overlap safely
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    def upsert_job(
        self,
        job_id: str,
        job_name: str,
        metadata_path: str,
        status: str = "NEW",
        slurm_job_id: Optional[int] = None,
        submitted_at: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO jobs
                    (job_id, job_name, metadata_path, status, slurm_job_id, submitted_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(job_id) DO UPDATE SET
                    job_name      = excluded.job_name,
                    metadata_path = excluded.metadata_path,
                    status        = excluded.status,
                    slurm_job_id  = COALESCE(excluded.slurm_job_id, jobs.slurm_job_id),
                    submitted_at  = COALESCE(excluded.submitted_at, jobs.submitted_at),
                    updated_at    = datetime('now')
                """,
                (job_id, job_name, metadata_path, status, slurm_job_id, submitted_at),
            )

    def mark_submitted(self, job_id: str, slurm_job_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET slurm_job_id = ?, status = 'PENDING',
                    submitted_at = datetime('now'), updated_at = datetime('now')
                WHERE job_id = ?
                """,
                (slurm_job_id, job_id),
            )

    def update_status(self, job_id: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE jobs SET status = ?, updated_at = datetime('now') WHERE job_id = ?",
                (status, job_id),
            )

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

    def get_job_by_metadata_path(self, metadata_path: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM jobs WHERE metadata_path = ?", (metadata_path,)
            ).fetchone()

    def list_jobs(self, status: Optional[str] = None) -> Iterable[sqlite3.Row]:
        with self._connect() as conn:
            if status:
                cur = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY updated_at DESC", (status,)
                )
            else:
                cur = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC")
            return cur.fetchall()
