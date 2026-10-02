"""Shared SQLite queue: a cross-job index of metadata paths and their
last-known SLURM status, so you can `SELECT * FROM jobs` for a dashboard
without opening every per-job JSON file individually.

This is a secondary index, not the source of truth for any one job -
the per-job metadata file (metadata.py) still owns that. That framing
is why lock contention here degrades gracefully rather than crashing:
a WRITE that can't land (upsert_job/update_status/mark_submitted) is
saved to a spool directory next to the db and applied by the next
process that gets through, so the dashboard is briefly stale, not
wrong - and the job itself is never unrecorded, since the metadata
file already has the real, authoritative result.

The db is usually in an NFS-mounted $HOME and written by jobs on many
compute nodes, so it runs in rollback-journal mode, never WAL (see
QueueDB._set_journal_mode), and a "malformed" error is double-checked
before the db is moved aside (QueueDB._confirm_corruption). A READ that can't complete (get_job/list_jobs/query_jobs)
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
import socket
import sqlite3
import sys
import time
import uuid
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
# Before treating a "malformed" error as real, look again after this long
# (see QueueDB._confirm_corruption).
QUEUE_DB_CORRUPTION_RECHECK_DELAY = 0.2  # seconds
QUEUE_DB_STALE_LOCK_SECONDS = 60
# Deferred writes applied per operation, so a long backlog can't turn one
# job_queue_run into a slow one; `job-queue flush` applies them all.
QUEUE_DB_SPOOL_BATCH = 200

# Rollback-journal modes are safe on a network filesystem; WAL is not
# (see QueueDB._set_journal_mode). JOB_QUEUE_JOURNAL_MODE=WAL is for a
# queue db on a local disk used from one machine.
JOURNAL_MODES = ("delete", "truncate", "persist", "wal")
DEFAULT_JOURNAL_MODE = "delete"


def _journal_mode(requested: Optional[str] = None) -> str:
    mode = (requested or os.environ.get("JOB_QUEUE_JOURNAL_MODE") or DEFAULT_JOURNAL_MODE).strip().lower()
    if mode not in JOURNAL_MODES:
        print(
            f"job-queue: ignoring unknown journal mode {mode!r} (use one of {', '.join(JOURNAL_MODES)})",
            file=sys.stderr,
        )
        return DEFAULT_JOURNAL_MODE
    return mode


def _utc_now() -> str:
    # same format as SQLite's datetime('now'), so the two compare as strings
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


class QueueUnavailable(RuntimeError):
    """Raised only for READ operations (get_job/list_jobs/query_jobs/
    etc.) that could not complete after retrying a locked queue db -
    i.e. the caller genuinely can't get an answer, as opposed to a
    write that can be safely skipped since the per-job metadata file
    already has the real result.
    """


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower()

def _is_corruption_error(exc: BaseException) -> bool:
    """SQLite's own wording for on-disk corruption. Raised by the
    sqlite3 module as a plain DatabaseError - NOT OperationalError -
    so catching only OperationalError (as the lock-retry logic already
    did) let this straight through, uncaught. Never a transient
    condition worth retrying as-is: the file itself is broken, so
    recovery means recreating it, not waiting and trying again.
    """
    msg = str(exc).lower()
    return "malformed" in msg or "file is not a database" in msg

def _is_transient_io_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "disk i/o error" in msg or "unable to open database file" in msg


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
    def __init__(self, db_path: Optional[Path] = None, journal_mode: Optional[str] = None):
        ensure_queue_dir()
        self.db_path = Path(db_path) if db_path else QUEUE_DB_PATH
        # Writes that can't land are kept here (one JSON file per write)
        # and applied by the next process that gets through - see _spool.
        self.spool_dir = self.db_path.with_name(self.db_path.name + ".spool")
        self.journal_mode = _journal_mode(journal_mode)
        self._journal_warned = False
        # Schema init never raises for lock contention - if it can't
        # get through after retrying, every subsequent write is spooled
        # (with a warning) and every read raises QueueUnavailable,
        # rather than the constructor itself crashing every command
        # that happens to instantiate a QueueDB
        # (status/record-submission/record-result all do).
        self._available = self._try_init_schema()

    def _init_schema_once(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)
        # CREATE TABLE IF NOT EXISTS won't add columns to a jobs table
        # that already existed before this version added `metadata` -
        # migrate those in place, once, idempotently.
        existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if "metadata" not in existing_cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN metadata TEXT")

    def _retrying(self, fn):
        """Runs fn() with the lock / transient-I/O / corruption handling
        shared by every operation. Returns (True, result) on success and
        (False, last_exc) when retries ran out; any other error raises.
        """
        delay = QUEUE_DB_LOCK_INITIAL_BACKOFF
        last_exc: Optional[BaseException] = None
        for _ in range(QUEUE_DB_LOCK_RETRIES):
            try:
                return True, fn()
            except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                if not (_is_corruption_error(exc) or _is_locked_error(exc) or _is_transient_io_error(exc)):
                    raise
                last_exc = exc
                if _is_corruption_error(exc) and self._quarantine_corrupt_db(exc):
                    continue  # a fresh db is in place; retry at once
                # locked, a transient NFS error, or "corruption" that a
                # second look didn't confirm
                time.sleep(delay)
                delay = min(delay * 2, QUEUE_DB_LOCK_MAX_BACKOFF)
        return False, last_exc

    def _try_init_schema(self) -> bool:
        def _do():
            with self._connect() as conn:
                self._init_schema_once(conn)

        ok, last_exc = self._retrying(_do)
        if not ok:
            print(
                f"job-queue: queue db is locked, continuing without the shared index "
                f"({last_exc}); per-job metadata is unaffected",
                file=sys.stderr,
            )
        return ok

    def _confirm_corruption(self) -> bool:
        """A "malformed" error on NFS is often a stale client cache of a
        file another node just rewrote, not real damage - and moving the
        shared db aside on a false alarm throws away every other node's
        history and splits them across two files. So look again, with a
        fresh connection, twice, before believing it.
        """
        for attempt in range(2):
            if attempt:
                time.sleep(QUEUE_DB_CORRUPTION_RECHECK_DELAY)
            try:
                conn = sqlite3.connect(self.db_path, timeout=30)
                try:
                    if conn.execute("PRAGMA quick_check").fetchone()[0] == "ok":
                        return False
                finally:
                    conn.close()
            except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
                if not _is_corruption_error(exc):
                    return False  # locked / unreachable: can't tell, so don't act
        return True

    def _quarantine_corrupt_db(self, exc: BaseException) -> bool:
        """Move a db that is confirmed corrupt (and any -wal/-shm
        sidecars) aside for forensics, clearing the way for a fresh one.
        Returns True if the caller should retry against a fresh db.

        Safe to do: this db is a secondary index, never the source of
        truth for any one job's outcome (see module docstring) - losing
        its history means a dashboard that's briefly starting over, not
        lost work. Only one process does the move (an mkdir lock, which
        is atomic on NFS too); the others wait and retry.
        """
        if not self.db_path.exists():
            return True
        if not self._confirm_corruption():
            return False
        lock = self.db_path.with_name(self.db_path.name + ".quarantine-lock")
        try:
            lock.mkdir()
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > QUEUE_DB_STALE_LOCK_SECONDS:
                    lock.rmdir()  # left by a process that was killed mid-move
            except OSError:
                pass
            return False  # someone else is on it
        except OSError:
            return False
        try:
            if not self._confirm_corruption():  # another node may have replaced it already
                return True
            stamp = time.strftime("%Y%m%dT%H%M%S")
            print(
                f"job-queue: queue db appears corrupted ({exc}); "
                f"quarantining it and starting a fresh one. Per-job metadata is unaffected.",
                file=sys.stderr,
            )
            for suffix in ("", "-wal", "-shm", "-journal"):
                src = self.db_path.with_name(self.db_path.name + suffix)
                if not src.exists():
                    continue
                dst = self.db_path.with_name(f"{self.db_path.name}{suffix}.corrupt-{stamp}")
                try:
                    src.rename(dst)
                except OSError as rename_exc:
                    print(
                        f"job-queue: couldn't quarantine {src} ({rename_exc}); removing it instead",
                        file=sys.stderr,
                    )
                    try:
                        src.unlink()
                    except OSError:
                        pass
            return True
        finally:
            try:
                lock.rmdir()
            except OSError:
                pass

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            self._set_journal_mode(conn)
            conn.row_factory = sqlite3.Row
            # Registered per-connection (sqlite3 doesn't share custom
            # functions across connections) so query_jobs() can express
            # name/dir regex filtering as ordinary SQL predicates rather
            # than fetching every row and filtering in Python.
            conn.create_function("REGEXP", 2, _sql_regexp)
            conn.create_function("JOB_DIR", 1, _sql_job_dir)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _set_journal_mode(self, conn: sqlite3.Connection) -> None:
        """Rollback-journal (DELETE) mode by default, never WAL unless
        asked for. WAL keeps its index in a memory-mapped -shm file, so
        every process using the db has to be on the same host - SQLite
        documents that it does not work over a network filesystem. A
        queue db in an NFS $HOME is written by jobs on many compute
        nodes, and there `PRAGMA journal_mode=WAL` usually *succeeds*
        (SQLite can't tell the filesystem is remote), the mode sticks to
        the file, and the nodes then read and write through separate
        -shm indexes: lost writes, "disk I/O error", and "database disk
        image is malformed". A db left in WAL mode by an earlier version
        is switched back here, which works once no other process has it
        open.
        """
        current = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
        if current == self.journal_mode:
            return
        try:
            current = conn.execute(f"PRAGMA journal_mode={self.journal_mode}").fetchone()[0].lower()
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as exc:
            if _is_corruption_error(exc):
                raise
        if current != self.journal_mode and not self._journal_warned:
            self._journal_warned = True
            print(
                f"job-queue: {self.db_path} is in {current} mode and could not be switched to "
                f"{self.journal_mode} (another process has it open); it will be on a later run",
                file=sys.stderr,
            )

    # -- writes ---------------------------------------------------------

    def _write(self, op: str, args: dict) -> None:
        """Applies one write, retrying briefly on lock contention. If it
        still can't get through, the write is saved to the spool
        directory rather than dropped, and the next process that reaches
        the db applies it - so a busy or briefly unreachable db leaves
        the dashboard late, not permanently wrong. Never raises for
        contention, since the job's own metadata file already has the
        real result.
        """
        at = _utc_now()
        if self._available:
            def _do():
                with self._connect() as conn:
                    self._drain_spool(conn)
                    self._apply(conn, op, args, at)

            ok, last_exc = self._retrying(_do)
            if ok:
                return
            reason = f"queue db still locked ({last_exc})"
        else:
            reason = "queue db unavailable"
        if self._spool(op, args, at):
            print(f"job-queue: {op} deferred - {reason}; saved to {self.spool_dir}", file=sys.stderr)
        else:
            print(f"job-queue: {op} failed - {reason}", file=sys.stderr)

    def _apply(self, conn, op: str, args: dict, at: str, replay: bool = False) -> None:
        if replay:
            # A deferred write must not undo a newer one that got
            # through in the meantime.
            row = conn.execute(
                "SELECT updated_at FROM jobs WHERE job_id = ? OR metadata_path = ?",
                (args.get("job_id"), args.get("metadata_path")),
            ).fetchone()
            if row is not None and row["updated_at"] and row["updated_at"] > at:
                return
        getattr(self, "_op_" + op)(conn, at=at, **args)

    def _spool(self, op: str, args: dict, at: str) -> bool:
        """Saves one write as its own file: created under a temporary
        name and renamed into place, which is atomic on NFS as well, so
        a reader never sees half of one and writers never contend.
        """
        name = f"{time.time_ns():020d}-{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        try:
            self.spool_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.spool_dir / (name + ".tmp")
            tmp.write_text(json.dumps({"op": op, "args": args, "at": at}))
            os.replace(tmp, self.spool_dir / (name + ".json"))
            return True
        except OSError:
            return False

    def _drain_spool(self, conn, limit: int = QUEUE_DB_SPOOL_BATCH) -> int:
        """Applies deferred writes, oldest first, inside the caller's
        transaction; the files are removed only after it commits, so a
        crash part way just means they are applied again (every write is
        an idempotent upsert/update).
        """
        try:
            files = sorted(self.spool_dir.glob("*.json"))[:limit]
        except OSError:
            return 0
        if not files:
            return 0
        applied = []
        for f in files:
            try:
                event = json.loads(f.read_text())
                op, args, at = event["op"], event["args"], event["at"]
                if not hasattr(self, "_op_" + op):
                    raise ValueError(f"unknown op {op!r}")
            except FileNotFoundError:
                continue  # another process applied it
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f"job-queue: setting aside unreadable spool file {f} ({exc})", file=sys.stderr)
                try:
                    os.replace(f, f.with_suffix(".bad"))
                except OSError:
                    pass
                continue
            self._apply(conn, op, args, at, replay=True)
            applied.append(f)
        conn.commit()
        for f in applied:
            try:
                f.unlink()
            except OSError:
                pass
        return len(applied)

    def flush_spool(self) -> int:
        """Applies every deferred write now; returns how many. Raises
        QueueUnavailable if the db can't be reached.
        """
        total = 0

        def _do():
            with self._connect() as conn:
                return self._drain_spool(conn)

        while True:
            if not self._available:
                self._available = self._try_init_schema()
            if not self._available:
                raise QueueUnavailable("queue db unavailable")
            ok, result = self._retrying(_do)
            if not ok:
                raise QueueUnavailable(f"queue db still locked after retrying: {result}") from result
            total += result
            if result < QUEUE_DB_SPOOL_BATCH:
                return total

    def spooled(self) -> int:
        """How many deferred writes are waiting."""
        try:
            return sum(1 for _ in self.spool_dir.glob("*.json"))
        except OSError:
            return 0

    def _run_read(self, fn):
        """Same retry loop as _write, but raises QueueUnavailable on
        exhaustion instead of deferring - a read genuinely cannot
        answer its caller's question if it can't get through. Deferred
        writes are applied first, so the answer is current.
        """
        if not self._available:
            self._available = self._try_init_schema()
            if not self._available:
                raise QueueUnavailable("queue db unavailable (failed to initialize schema)")

        def _do():
            if self.spooled():
                with self._connect() as conn:
                    self._drain_spool(conn)
            return fn()

        ok, result = self._retrying(_do)
        if not ok:
            raise QueueUnavailable(f"queue db still locked after retrying: {result}") from result
        return result

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
        self._write("upsert_job", dict(
            job_id=job_id, job_name=job_name, metadata_path=metadata_path, status=status,
            slurm_job_id=slurm_job_id, submitted_at=submitted_at, metadata_json=metadata_json,
        ))

    def _op_upsert_job(self, conn, *, at, job_id, job_name, metadata_path, status,
                       slurm_job_id, submitted_at, metadata_json) -> None:
        try:
            conn.execute(
                """
                INSERT INTO jobs
                    (job_id, job_name, metadata_path, status, slurm_job_id, submitted_at, updated_at, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    job_name      = excluded.job_name,
                    metadata_path = excluded.metadata_path,
                    status        = excluded.status,
                    slurm_job_id  = COALESCE(excluded.slurm_job_id, jobs.slurm_job_id),
                    submitted_at  = COALESCE(excluded.submitted_at, jobs.submitted_at),
                    metadata      = COALESCE(excluded.metadata, jobs.metadata),
                    updated_at    = excluded.updated_at
                """,
                (job_id, job_name, metadata_path, status, slurm_job_id, submitted_at, at, metadata_json),
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
                    updated_at   = ?
                WHERE metadata_path = ?
                """,
                (job_name, status, slurm_job_id, submitted_at, metadata_json, at, metadata_path),
            )

    def mark_submitted(self, job_id: str, slurm_job_id: int) -> None:
        self._write("mark_submitted", dict(job_id=job_id, slurm_job_id=slurm_job_id))

    def _op_mark_submitted(self, conn, *, at, job_id, slurm_job_id) -> None:
        conn.execute(
            """
            UPDATE jobs
            SET slurm_job_id = ?, status = 'PENDING', submitted_at = ?, updated_at = ?
            WHERE job_id = ?
            """,
            (slurm_job_id, at, at, job_id),
        )

    def update_status(
        self, job_id: str, status: str, metadata_json: Optional[str] = None
    ) -> None:
        self._write("update_status", dict(job_id=job_id, status=status, metadata_json=metadata_json))

    def _op_update_status(self, conn, *, at, job_id, status, metadata_json) -> None:
        conn.execute(
            """
            UPDATE jobs
            SET status = ?, metadata = COALESCE(?, metadata), updated_at = ?
            WHERE job_id = ?
            """,
            (status, metadata_json, at, job_id),
        )

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
