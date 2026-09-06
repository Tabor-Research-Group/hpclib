"""Parsing/writing support for job_process_queue (lib/job_queue.sh) -
turns one JSON file describing many jobs into one, fully isolated
per-job directory: config.json plus its own copy of the shared
process_sbatch.sh, so each job runs its own script instance rather
than N sbatch invocations racing to read the same file out from under
each other (e.g. if it's edited/replaced mid-sweep).

No submission or status logic lives here - that's job_process_queue's
job (decides whether to sbatch at all) and job_queue_run's job (decides
submit vs. restart once running), both in lib/job_queue.sh. This module
only ever reads and writes plain files.
"""
import json
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Union

PathLike = Union[str, Path]


@dataclass
class JobSpec:
    """One entry from the batch JSON file."""
    job_name: str
    config: dict = field(default_factory=dict)


@dataclass
class JobDir:
    job_name: str
    job_dir: Path
    config_path: Path
    sbatch_script: Path


def load_job_specs(path: PathLike) -> List[JobSpec]:
    """Parse the batch JSON file.

    Expected shape - a JSON array of objects, each requiring a
    `job_name` key; every other key on that object is per-job config,
    written out verbatim to that job's own config file:

        [
          {"job_name": "run-a", "lr": 0.001, "epochs": 50},
          {"job_name": "run-b", "lr": 0.01,  "epochs": 50}
        ]

    A `{"jobs": [...]}` wrapper is also accepted, for callers who'd
    rather not have a bare top-level array.
    """
    with open(path) as f:
        raw = json.load(f)

    if isinstance(raw, dict) and "jobs" in raw:
        raw = raw["jobs"]
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a JSON array of job entries (or {{'jobs': [...]}})")

    specs = []
    seen = set()
    for i, entry in enumerate(raw):
        if "job_name" not in entry:
            raise ValueError(f"{path}: entry {i} is missing required key 'job_name'")
        job_name = entry["job_name"]
        if job_name in seen:
            raise ValueError(f"{path}: duplicate job_name '{job_name}'")
        seen.add(job_name)
        specs.append(JobSpec(job_name=job_name, config={k: v for k, v in entry.items() if k != "job_name"}))
    return specs


def write_job_dir(spec: JobSpec, config_dir: PathLike, sbatch_script: PathLike) -> JobDir:
    """Set up one job's isolated directory: config_dir/job_name/,
    containing config.json (this job's own slice of the batch file)
    and its own copy of the shared process_sbatch.sh - so a batch
    re-run overwriting one job's directory can't race a DIFFERENT
    job's in-flight sbatch process still reading the same script or
    config file out from under it.

    Returns the paths sbatch needs: the copied script (what actually
    gets sbatch'd) and the config file (its one command-line argument).
    """
