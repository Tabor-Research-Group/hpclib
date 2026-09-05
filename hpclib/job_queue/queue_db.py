"""Shared SQLite queue: a cross-job index of metadata paths and their
last-known SLURM status, so you can `SELECT * FROM jobs` for a dashboard
without opening every per-job JSON file individually.

This is a secondary index, not the source of truth for any one job -
the per-job metadata file (metadata.py) still owns that. That framing
is why lock contention here degrades gracefully rather than crashing:
a WRITE that can't land (upsert_job/update_status/mark_submitted) just
means the dashboard is briefly stale, not that the job itself is
unrecorded - the metadata file already has the real, authoritative
result. A READ that can't complete (get_job/list_jobs) genuinely can't
answer the caller's question, so those raise QueueUnavailable instead
of silently returning nothing - callers like `job-queue list` are
expected to catch it and print a clean one-line message.
"""
import sqlite3
import sys
import time
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

# How hard to retry before giving up on a locked db. Kept short (well
# under a second worst case) since this sits on the fast path of every
# job_queue_run invocation - it should never turn a busy queue db into
# a slow sbatch script.
QUEUE_DB_LOCK_RETRIES = 5
QUEUE_DB_LOCK_INITIAL_BACKOFF = 0.05  # seconds
QUEUE_DB_LOCK_MAX_BACKOFF = 0.5       # seconds


class QueueUnavailable(RuntimeError):
    """Raised only for READ operations (get_job/list_jobs/etc.) that
    could not complete after retrying a locked queue db - i.e. the
    caller genuinely can't get an answer, as opposed to a write that
    can be safely skipped since the per-job metadata file already has
    the real result.
    """


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower()


class QueueDB:
    def __init__(self, db_path: Optional[Path] = None):
        ensure_queue_dir()
        self.db_path = Path(db_path) if db_path else QUEUE_DB_PATH
        # Schema init never raises for lock contention - if it can't
        # get through after retrying, every subsequent write becomes a
        # no-op (with a one-time warning) and every read raises
        # QueueUnavailable, rather than the constructor itself
        # crashing every command that happens to instantiate a
        # QueueDB (status/record-submission/record-result all do).
        self._available = self._try_init_schema()

    def _try_init_schema(self) -> bool:
        delay = QUEUE_DB_LOCK_INITIAL_BACKOFF
        last_exc: Optional[sqlite3.OperationalError] = None
        for _ in range(QUEUE_DB_LOCK_RETRIES):
            try:
                with self._connect() as conn:
                    conn.executescript(SCHEMA)
                return True
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc):
                    raise
                last_exc = exc
                time.sleep(delay)
                delay = min(delay * 2, QUEUE_DB_LOCK_MAX_BACKOFF)
        print(
            f"job-queue: queue db is locked, continuing without the shared index "
            f"({last_exc}); per-job metadata is unaffected",
            file=sys.stderr,
        )
        return False

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

    def _run_write(self, label: str, fn) -> None:
        """Runs a write operation, retrying briefly on lock
        contention. If it still can't get through, warns once to
        stderr and returns - never raises - since a missed write here
        only means the shared dashboard is briefly stale, not that the
        job's own outcome went unrecorded.
        """
        if not self._available:
            print(f"job-queue: skipping {label} - queue db unavailable", file=sys.stderr)
            return
        delay = QUEUE_DB_LOCK_INITIAL_BACKOFF
        last_exc: Optional[sqlite3.OperationalError] = None
        for _ in range(QUEUE_DB_LOCK_RETRIES):
            try:
                fn()
                return
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc):
                    raise
                last_exc = exc
                time.sleep(delay)
                delay = min(delay * 2, QUEUE_DB_LOCK_MAX_BACKOFF)
        print(f"job-queue: {label} failed, queue db still locked ({last_exc})", file=sys.stderr)

    def _run_read(self, fn):
        """Same retry loop as _run_write, but raises QueueUnavailable
        on exhaustion instead of swallowing the failure - a read
        genuinely cannot answer its caller's question if it can't get
        through.
        """
        if not self._available:
            raise QueueUnavailable("queue db unavailable (failed to initialize schema)")
        delay = QUEUE_DB_LOCK_INITIAL_BACKOFF
        last_exc: Optional[sqlite3.OperationalError] = None
        for _ in range(QUEUE_DB_LOCK_RETRIES):
            try:
                return fn()
            except sqlite3.OperationalError as exc:
                if not _is_locked_error(exc):
                    raise
                last_exc = exc
                time.sleep(delay)
                delay = min(delay * 2, QUEUE_DB_LOCK_MAX_BACKOFF)
        raise QueueUnavailable(f"queue db still locked after retrying: {last_exc}") from last_exc

    def upsert_job(
        self,
        job_id: str,
        job_name: str,
        metadata_path: str,
        status: str = "NEW",
        slurm_job_id: Optional[int] = None,
        submitted_at: Optional[str] = None,
    ) -> None:
        def _do():
            with self._connect() as conn:
                try:
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
                except sqlite3.IntegrityError:
                    # Only reachable if two DIFFERENT job_ids somehow
                    # got attached to the SAME metadata_path
                    # (metadata.py's own load_or_create lock is meant
                    # to prevent this, but e.g. an NFS mount that
                    # doesn't honor flock could still let it through).
                    # metadata_path is the real source of truth for
                    # "which file is this," so fold into that existing
                    # row instead of crashing the caller.
                    conn.execute(
                        """
                        UPDATE jobs SET
                            job_name     = ?,
                            status       = ?,
                            slurm_job_id = COALESCE(?, slurm_job_id),
                            submitted_at = COALESCE(?, submitted_at),
                            updated_at   = datetime('now')
                        WHERE metadata_path = ?
                        """,
                        (job_name, status, slurm_job_id, submitted_at, metadata_path),
                    )

        self._run_write("upsert_job", _do)

    def mark_submitted(self, job_id: str, slurm_job_id: int) -> None:
        def _do():
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

        self._run_write("mark_submitted", _do)

    def update_status(self, job_id: str, status: str) -> None:
        def _do():
            with self._connect() as conn:
                conn.execute(
                    "UPDATE jobs SET status = ?, updated_at = datetime('now') WHERE job_id = ?",
                    (status, job_id),
                )

        self._run_write("update_status", _do)

    def get_job(self, job_id: str) -> Optional[sqlite3.Row]:
        def _do():
            with self._connect() as conn:
                return conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()

        return self._run_read(_do)

    def get_job_by_metadata_path(self, metadata_path: str) -> Optional[sqlite3.Row]:
        def _do():
            with self._connect() as conn:
                return conn.execute(
                    "SELECT * FROM jobs WHERE metadata_path = ?", (metadata_path,)
                ).fetchone()

        return self._run_read(_do)

    def list_jobs(self, status: Optional[str] = None) -> Iterable[sqlite3.Row]:
        def _do():
            with self._connect() as conn:
                if status:
                    cur = conn.execute(
                        "SELECT * FROM jobs WHERE status = ? ORDER BY updated_at DESC", (status,)
                    )
                else:
                    cur = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC")
                return cur.fetchall()

        return self._run_read(_do)
