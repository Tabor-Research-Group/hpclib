"""Requirement (1): the single function that decides submit vs. restart
vs. complete vs. error for a job, given only its metadata file path.

This is the one place that logic lives - job_queue_status.py (the CLI
entrypoint sbatch scripts call) is a thin wrapper around it, and any
future dashboard/monitor code should call this too rather than
re-deriving the rules.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .metadata import MetadataStore
from .models import ACTIVE_STATES, TERMINAL_FAILURE_STATES, JobStatus
from .queue_db import QueueDB
from .sacct import query_sacct


@dataclass
class Decision:
    action: str  # "submit" | "restart" | "running" | "complete" | "error"
    job_id: Optional[str] = None
    detail: Optional[str] = None


def determine_action(
    metadata_path: str,
    job_name: Optional[str] = None,
    job_id: Optional[str] = None,
    queue: Optional[QueueDB] = None,
) -> Decision:
    """Decide what a job should do next.

    - No metadata file yet, or metadata exists but was never submitted
      -> "submit"
    - Previously submitted and SLURM reports it still active
      -> "running" (safety case: script re-invoked on an in-flight job)
    - Previously submitted and SLURM reports success
      -> "complete"
    - Previously submitted, SLURM reports a failure state, and attempts
      remain -> "restart"
    - Previously submitted, failed, and attempts are exhausted
      -> "error"
    """
    store = MetadataStore(metadata_path)
    queue = queue or QueueDB()
    resolved_path = str(Path(metadata_path).resolve())

    # --- Never launched before --------------------------------------
    if not store.exists():
        meta = store.load_or_create(job_name=job_name, job_id=job_id)
        queue.upsert_job(
            job_id=meta.job_id,
            job_name=meta.job_name,
            metadata_path=resolved_path,
            status=JobStatus.NEW.value,
        )
        return Decision(action="submit", job_id=meta.job_id)

    meta = store.load_or_create(job_name=job_name, job_id=job_id)

    # Metadata file exists but sbatch was never actually called for it yet
    if meta.slurm_job_id is None:
        queue.upsert_job(
            job_id=meta.job_id,
            job_name=meta.job_name,
            metadata_path=resolved_path,
            status=JobStatus.NEW.value,
        )
        return Decision(action="submit", job_id=meta.job_id)

    # --- Previously launched: ask SLURM for the real status ----------
    live_status, exit_code, raw_state = query_sacct(meta.slurm_job_id)
    if live_status is JobStatus.UNKNOWN:
        # sacct couldn't tell us anything new; trust the metadata file's
        # last-recorded status instead of silently treating it as done.
        live_status = meta.status

    queue.update_status(meta.job_id, live_status.value)

    if live_status in ACTIVE_STATES:
        return Decision(
            action="running", job_id=meta.job_id,
            detail=f"SLURM state: {live_status.value}",
        )

    if live_status is JobStatus.COMPLETED:
        store.update(status=JobStatus.COMPLETED, exit_code=exit_code)
        return Decision(action="complete", job_id=meta.job_id)

    if live_status in TERMINAL_FAILURE_STATES:
        error_msg = meta.error_message or f"SLURM reported state {raw_state or live_status.value}"
        store.update(status=live_status, exit_code=exit_code, error_message=error_msg)

        if meta.attempt >= meta.max_attempts:
            return Decision(
                action="error",
                job_id=meta.job_id,
                detail=f"{error_msg} (exhausted {meta.attempt}/{meta.max_attempts} attempts)",
            )
        return Decision(action="restart", job_id=meta.job_id, detail=error_msg)

    # Anything unmapped (shouldn't normally happen) -> surface as error
    # rather than guessing.
    return Decision(
        action="error",
        job_id=meta.job_id,
        detail=f"Unrecognized SLURM state: {raw_state or live_status.value}",
    )
