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
import json

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
            metadata_json=json.dumps(meta.to_dict()),
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
            metadata_json=json.dumps(meta.to_dict()),
        )
        return Decision(action="submit", job_id=meta.job_id)

    # --- Previously launched: ask SLURM for the real status ----------
    live_status, exit_code, raw_state = query_sacct(meta.slurm_job_id)
    if live_status is JobStatus.UNKNOWN:
        # sacct couldn't tell us anything new; trust the metadata file's
        # last-recorded status instead of silently treating it as done.
        live_status = meta.status

    # upsert_job (not update_status) from here on: the queue db is a
    # secondary index and its own writes can be silently skipped under
    # lock contention (see queue_db.py's graceful-degradation
    # behavior), so a row can legitimately be missing here even though
    # this job has definitely been submitted before. update_status is
    # a plain UPDATE ... WHERE job_id = ? - if the row doesn't exist,
    # that matches zero rows and does nothing, forever, since every
    # later call would ALSO just be an UPDATE. upsert_job self-heals
    # by creating the row if it's missing, same as the "never
    # launched" branches above already do.
    def _sync_queue(status_value: str) -> None:
        queue.upsert_job(
            job_id=meta.job_id,
            job_name=meta.job_name,
            metadata_path=resolved_path,
            status=status_value,
            slurm_job_id=meta.slurm_job_id,
            metadata_json=json.dumps(meta.to_dict()),
        )

    if live_status in ACTIVE_STATES:
        _sync_queue(live_status.value)
        return Decision(
            action="running", job_id=meta.job_id,
            detail=f"SLURM state: {live_status.value}",
        )

    if live_status is JobStatus.COMPLETED:
        meta = store.update(status=JobStatus.COMPLETED, exit_code=exit_code)
        _sync_queue(live_status.value)
        return Decision(action="complete", job_id=meta.job_id)

    if live_status in TERMINAL_FAILURE_STATES:
        error_msg = meta.error_message or f"SLURM reported state {raw_state or live_status.value}"
        meta = store.update(status=live_status, exit_code=exit_code, error_message=error_msg)
        _sync_queue(live_status.value)

        if meta.attempt >= meta.max_attempts:
            return Decision(
                action="error",
                job_id=meta.job_id,
                detail=f"{error_msg} (exhausted {meta.attempt}/{meta.max_attempts} attempts)",
            )
        return Decision(action="restart", job_id=meta.job_id, detail=error_msg)

    # Anything unmapped (shouldn't normally happen) -> surface as error
    # rather than guessing.
    _sync_queue(live_status.value)
    return Decision(
        action="error",
        job_id=meta.job_id,
        detail=f"Unrecognized SLURM state: {raw_state or live_status.value}",
    )
