from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class JobStatus(str, Enum):
    NEW = "NEW"                # metadata exists, never submitted to SLURM
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    NODE_FAIL = "NODE_FAIL"
    UNKNOWN = "UNKNOWN"


ACTIVE_STATES = {JobStatus.PENDING, JobStatus.RUNNING}
TERMINAL_FAILURE_STATES = {
    JobStatus.FAILED,
    JobStatus.TIMEOUT,
    JobStatus.CANCELLED,
    JobStatus.NODE_FAIL,
}


@dataclass
class JobMetadata:
    job_id: str                             # stable UUID, independent of SLURM's id
    job_name: str
    slurm_job_id: Optional[int] = None
    status: JobStatus = JobStatus.NEW
    attempt: int = 0
    max_attempts: int = 5
    script_last_used: Optional[str] = None
    first_submitted: Optional[str] = None
    last_submitted: Optional[str] = None
    exit_code: Optional[int] = None
    error_message: Optional[str] = None
    checkpoint_path: Optional[str] = None
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "JobMetadata":
        d = dict(d)
        raw_status = d.get("status", "NEW")
        try:
            status = JobStatus(raw_status)
        except ValueError:
            status = JobStatus.UNKNOWN
        d["status"] = status

        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        base = {k: v for k, v in d.items() if k in known}
        leftover = {k: v for k, v in d.items() if k not in known and k != "extra"}
        base["extra"] = {**d.get("extra", {}), **leftover}
        return cls(**base)
