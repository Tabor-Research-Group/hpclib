"""Shared SQLite queue: a cross-job index of metadata paths and their
last-known SLURM status, so you can `SELECT * FROM jobs` for a dashboard
without opening every per-job JSON file individually.

This is a secondary index, not the source of truth for any one job -
the per-job metadata file (metadata.py) still owns that. That framing
is why lock contention here degrades gracefully rather than crashing:
a WRITE that can't land (upsert_job/update_status/mark_submitted) just
means the dashboard is briefly stale, not that the job itself is
unrecorded - the metadata file already has the real, authoritative
result. A READ that can't complete (get_job/list_jobs/query_jobs)
genuinely can't answer the caller's question, so those raise
QueueUnavailable instead of silently returning nothing - callers like
`job-queue list` are expected to catch it and print a clean one-line
message.

The `metadata` column stores each job's full JobMetadata as a
serialized JSON blob (see upsert_job/update_status), so query_jobs()
below can filter/inspect arbitrary metadata fields (attempt count,
checkpoint_path, extra, ...) across every job without opening each
per-job JSON file individually - useful for deciding which jobs are
worth restarting without a separate pass over the filesystem.
"""
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional

from .config import QUEUE_DB_PATH, ensure_queue_dir
from .models import JobStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id          TEXT PRIMARY KEY,
    job_name        TEXT NOT NULL,
    metadata_path   TEXT NOT NULL UNIQUE,
    slurm_job_id    INTEGER,
    status          TEXT NOT NULL DEFAULT 'NEW',
    submitted_at    TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    metadata        TEXT
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
    """Raised only for READ operations (get_job/list_jobs/query_jobs/
    etc.) that could not complete after retrying a locked queue db -
    i.e. the caller genuinely can't get an answer, as opposed to a
    write that can be safely skipped since the per-job metadata file
    already has the real result.
    """


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower()


def _sql_regexp(pattern: str, value: Optional[str]) -> bool:
    # Registered as SQLite's REGEXP function: "X REGEXP Y" calls
    # regexp(Y, X) - i.e. (pattern, value) in that order, matching
    # this signature. Uses re.search (substring match), not
    # re.fullmatch, so "t2" matches "jq_t2_fail_then_restart" the way
    # someone filtering job names would expect.
    if value is None:
        return False
    return re.search(pattern, value) is not None


def _sql_job_dir(path: Optional[str]) -> str:
    # Registered as SQLite's JOB_DIR function - dirname of a
    # metadata_path, so dir_regex in query_jobs() below can filter on
    # the job's DIRECTORY specifically, not the full path (which also
    # contains the metadata filename itself).
    if not path:
        return ""
    return os.path.dirname(path)


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

    def _init_schema_once(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS won't add columns to a jobs table
        # that already existed before this version added `metadata` -
        # migrate those in place, once, idempotently.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "metadata" not in existing_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN metadata TEXT")

    def _try_init_schema(self) -> bool:
        delay = QUEUE_DB_LOCK_INITIAL_BACKOFF
        last_exc: Optional[sqlite3.OperationalError] = None
        for _ in range(QUEUE_DB_LOCK_RETRIES):
            try:
                with self._connect() as conn:
                    self._init_schema_once(conn)
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
        try:
            conn.execute("PRAGMA journal_mode=WAL")  # let readers/writers overlap safely
        except sqlite3.OperationalError:
            # WAL needs shared-memory/locking support that many NFS-mounted
            # $HOME filesystems don't provide (common on HPC clusters).
            # DELETE mode is slower under real concurrency but at least
            # doesn't crash outright - this is a secondary index anyway,
            # not the source of truth for any one job (see module docstring).
            conn.execute("PRAGMA journal_mode=DELETE")
        conn.row_factory = sqlite3.Row
        # Registered per-connection (sqlite3 doesn't share custom
        # functions across connections) so query_jobs() can express
        # name/dir regex filtering as ordinary SQL predicates rather
        # than fetching every row and filtering in Python.
        conn.create_function("REGEXP", 2, _sql_regexp)
        conn.create_function("JOB_DIR", 1, _sql_job_dir)
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
        metadata_json: Optional[str] = None,
    ) -> None:
        def _do():
            with self._connect() as conn:
                try:
                    conn.execute(
                        """
                        INSERT INTO jobs
                            (job_id, job_name, metadata_path, status, slurm_job_id, submitted_at, updated_at, metadata)
                        VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
                        ON CONFLICT(job_id) DO UPDATE SET
                            job_name      = excluded.job_name,
                            metadata_path = excluded.metadata_path,
                            status        = excluded.status,
                            slurm_job_id  = COALESCE(excluded.slurm_job_id, jobs.slurm_job_id),
                            submitted_at  = COALESCE(excluded.submitted_at, jobs.submitted_at),
                            metadata      = COALESCE(excluded.metadata, jobs.metadata),
                            updated_at    = datetime('now')
                        """,
                        (job_id, job_name, metadata_path, status, slurm_job_id, submitted_at, metadata_json),
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
                            metadata     = COALESCE(?, metadata),
                            updated_at   = datetime('now')
                        WHERE metadata_path = ?
                        """,
                        (job_name, status, slurm_job_id, submitted_at, metadata_json, metadata_path),
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

    def update_status(
        self, job_id: str, status: str, metadata_json: Optional[str] = None
    ) -> None:
        def _do():
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = ?, metadata = COALESCE(?, metadata), updated_at = datetime('now')
                    WHERE job_id = ?
                    """,
                    (status, metadata_json, job_id),
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
        """Kept for backward compatibility / the simple case - prefer
        query_jobs() for anything involving --incomplete or regex
        filtering.
        """
        return self.query_jobs(status=status)

    def query_jobs(
        self,
        *,
        status: Optional[str] = None,
        incomplete: bool = False,
        name_regex: Optional[str] = None,
        dir_regex: Optional[str] = None,
    ) -> Iterable[sqlite3.Row]:
        """The general-purpose query used by `job-queue list` and
        available directly to Python callers (e.g. deciding which
        directories still have work to restart without walking the
        filesystem).

        - incomplete=True: any status other than COMPLETED. Mutually
          exclusive with `status` (raises ValueError if both given -
          "give me everything incomplete" and "give me exactly this
          one status" are different questions and silently picking one
          over the other would be surprising).
        - name_regex / dir_regex: plain Python regex (re.search
          semantics - unanchored substring match), matched against
          job_name and against the DIRECTORY portion of metadata_path
          respectively (not the full path, which would also include
          the metadata filename). Invalid patterns raise re.error
          immediately, before touching the db, rather than surfacing
          as an opaque sqlite3 error from inside the registered
          REGEXP function.
        """
        if incomplete and status:
            raise ValueError("incomplete=True and status=... are mutually exclusive")

        # Validate up front - a bad pattern should fail clearly and
        # immediately, not as a wrapped sqlite3.OperationalError from
        # deep inside a retry loop.
        if name_regex is not None:
            re.compile(name_regex)
        if dir_regex is not None:
            re.compile(dir_regex)

        def _do():
            clauses = []
            params: list = []
            if incomplete:
                clauses.append("status != ?")
                params.append(JobStatus.COMPLETED.value)
            elif status:
                clauses.append("status = ?")
                params.append(status)
            if name_regex is not None:
                clauses.append("job_name REGEXP ?")
                params.append(name_regex)
            if dir_regex is not None:
                clauses.append("JOB_DIR(metadata_path) REGEXP ?")
                params.append(dir_regex)

            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            with self._connect() as conn:
                cur = conn.execute(f"SELECT * FROM jobs {where} ORDER BY updated_at DESC", params)
                return cur.fetchall()

        return self._run_read(_do)

    def get_metadata_dict(self, row: sqlite3.Row) -> Optional[dict]:
        """Convenience for callers of query_jobs()/list_jobs() who want
        the stored metadata blob as a dict rather than a raw JSON
        string. Returns None if this row predates the metadata column
        being populated (e.g. a job whose last write was before this
        feature existed).
        """
        raw = row["metadata"] if "metadata" in row.keys() else None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
