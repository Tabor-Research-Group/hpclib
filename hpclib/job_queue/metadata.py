"""Loader/updater for a single job's metadata JSON file.

One metadata file per job (not one shared file for all jobs) so that
concurrent SLURM jobs never contend for the same file lock. The shared
SQLite queue (queue_db.py) is where cross-job visibility lives instead.
"""
import fcntl  # POSIX-only; used to close a create-time race across
              # concurrent processes racing to initialize the same
              # job's metadata file (see load_or_create below). Fine
              # for login/compute nodes; not portable to plain
              # Windows, which this package has never targeted.
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .models import JobMetadata, JobStatus


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MetadataStore:
    """Reads/writes exactly one job's metadata file."""

    def __init__(self, path: os.PathLike):
        self.path = Path(path)

    def exists(self) -> bool:
        return self.path.exists()

    def load(self) -> Optional[JobMetadata]:
        if not self.exists():
            return None
        with open(self.path, "r") as f:
            data = json.load(f)
        return JobMetadata.from_dict(data)

    def load_or_create(
        self, job_name: Optional[str] = None, job_id: Optional[str] = None
    ) -> JobMetadata:
        """Load existing metadata if present, otherwise create it.

        If no job_id is supplied and none exists on disk, a UUID is
        generated here and persisted immediately so it stays stable
        across every future submit/restart cycle for this job.

        Guarded by a file lock so that two processes racing to create
        the SAME job's metadata at (almost) the same instant converge
        on one job_id instead of each minting its own - a bare
        check-then-act (load(), then save()) let both sides observe
        "doesn't exist yet" and independently generate different
        uuid4()s, which downstream showed up as a queue_db.py
        UNIQUE-constraint crash on metadata_path once both tried to
        upsert their own, different job_id against the same file path.
        """
        existing = self.load()
        if existing is not None:
            return existing

        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with open(lock_path, "w") as lock_f:
            fcntl.flock(lock_f, fcntl.LOCK_EX)
            try:
                # Re-check now that we hold the lock - another process
                # may have created the file (and released the lock)
                # while we were waiting for it, and we want ITS
                # job_id, not a freshly-generated one of our own.
                existing = self.load()
                if existing is not None:
                    return existing

                resolved_id = job_id or str(uuid.uuid4())
                meta = JobMetadata(
                    job_id=resolved_id,
                    job_name=job_name or self.path.stem,
                    status=JobStatus.NEW,
                )
                self.save(meta)
                return meta
            finally:
                fcntl.flock(lock_f, fcntl.LOCK_UN)

    def save(self, meta: JobMetadata) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(meta.to_dict(), f, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)  # atomic rename on POSIX filesystems
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    def update(self, **fields) -> JobMetadata:
        meta = self.load()
        if meta is None:
            raise FileNotFoundError(f"No metadata file at {self.path}")
        for key, value in fields.items():
            setattr(meta, key, value)
        self.save(meta)
        return meta

    def record_submission(self, slurm_job_id: int, script: str) -> JobMetadata:
        """Called right after `sbatch --parsable ...` succeeds."""
        meta = self.load()
        if meta is None:
            raise FileNotFoundError(f"No metadata file at {self.path}")

        now = _utcnow()
        meta.slurm_job_id = slurm_job_id
        meta.status = JobStatus.PENDING
        meta.script_last_used = script
        meta.attempt += 1
        meta.last_submitted = now
        if meta.first_submitted is None:
            meta.first_submitted = now
        meta.exit_code = None
        meta.error_message = None
        self.save(meta)
        return meta

    def record_result(
            self, status: JobStatus, exit_code: Optional[int] = None, error_message: Optional[str] = None
    ) -> JobMetadata:
        """Called once submit()/restart() has actually finished, with a
        real exit code already in hand. Deliberately independent of
        sacct.query_sacct()'s polling - the caller already knows the true
        outcome, so there's no need to wait for or trust SLURM accounting,
        which can lag or (per sacct.py's own fallback) be entirely
        unavailable in a test environment.
        """
        meta = self.load()
        if meta is None:
            raise FileNotFoundError(f"No metadata file at {self.path}")
        meta.status = status
        meta.exit_code = exit_code
        meta.error_message = error_message
        self.save(meta)
        return meta