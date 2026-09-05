"""Thin wrapper around `sacct` for pulling ground-truth job status."""
import subprocess
from typing import Optional, Tuple

from .models import JobStatus

_SACCT_STATE_MAP = {
    "PENDING": JobStatus.PENDING,
    "RUNNING": JobStatus.RUNNING,
    "COMPLETED": JobStatus.COMPLETED,
    "FAILED": JobStatus.FAILED,
    "TIMEOUT": JobStatus.TIMEOUT,
    "CANCELLED": JobStatus.CANCELLED,
    "NODE_FAIL": JobStatus.NODE_FAIL,
    "OUT_OF_MEMORY": JobStatus.FAILED,
    "PREEMPTED": JobStatus.CANCELLED,
}


def query_sacct(slurm_job_id: int) -> Tuple[JobStatus, Optional[int], Optional[str]]:
    """Return (status, exit_code, raw_state) for a SLURM job id.

    Falls back to (UNKNOWN, None, None) if `sacct` isn't on PATH or the
    job id isn't found (e.g. purged accounting history, or no real
    SLURM cluster present) — callers should fall back to the metadata
    file's last-recorded status in that case rather than trusting this
    blindly.
    """
    try:
        result = subprocess.run(
            [
                "sacct", "-j", str(slurm_job_id),
                "--format=State,ExitCode", "--noheader", "--parsable2",
            ],
            capture_output=True, text=True, timeout=15, check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return JobStatus.UNKNOWN, None, None

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return JobStatus.UNKNOWN, None, None

    # First line is the parent job step. State can read "CANCELLED by 1234"
    # so only the first whitespace-separated token is the actual state.
    fields = lines[0].split("|")
    state_raw = fields[0] if len(fields) > 0 else ""
    exit_raw = fields[1] if len(fields) > 1 else ""

    state_token = state_raw.split()[0] if state_raw else ""
    status = _SACCT_STATE_MAP.get(state_token, JobStatus.UNKNOWN)

    exit_code = None
    if exit_raw:
        try:
            exit_code = int(exit_raw.split(":")[0])
        except ValueError:
            exit_code = None

    return status, exit_code, state_raw or None
