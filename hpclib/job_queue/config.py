"""Central location for filesystem paths used by the job-queue package.

Override JOB_QUEUE_HOME in the environment if you want the queue database
to live somewhere other than the XDG-style default (e.g. for testing, or
to share a queue across a cluster on a shared filesystem).

The db runs in SQLite's rollback-journal (DELETE) mode, which is safe on
NFS; JOB_QUEUE_JOURNAL_MODE=WAL is only for a queue db on a local disk
used from a single machine. `job-queue check` reports which mode and
filesystem the db is on.
"""
from pathlib import Path
import os

QUEUE_DIR = Path(
    os.environ.get("JOB_QUEUE_HOME", Path.home() / ".local" / "share" / "job-queue")
)
QUEUE_DB_PATH = QUEUE_DIR / "queue.db"


def ensure_queue_dir() -> Path:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    return QUEUE_DIR
