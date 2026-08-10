# job-queue

An installable Python package for SLURM submit/restart/checkpoint state
tracking. Install it once in a standard location (a shared conda env, a
cluster-wide venv, `pipx`, etc.), and every project just calls the
`job-queue` command - no more copying `.py` files alongside each job's
shell scripts.

## Install

```bash
pip install -e /path/to/job-queue-pkg
# or, once published: pip install job-queue
```

This puts a `job-queue` console script on `PATH` (and `python -m job_queue`
works identically, if you'd rather invoke it that way).

## Layout

```
pyproject.toml
src/job_queue/
├── __init__.py        public API: JobMetadata, JobStatus, MetadataStore, QueueDB, determine_action
├── __main__.py         enables `python -m job_queue`
├── cli.py              argparse subcommands, entry point for the `job-queue` console script
├── config.py           paths (queue.db lives at ~/.local/share/job-queue/queue.db by default)
├── models.py           JobStatus enum + JobMetadata dataclass
├── metadata.py         per-job metadata JSON loader/updater, atomic writes, UUID assignment
├── queue_db.py         shared SQLite queue: job_id/name/metadata_path/status/slurm_job_id
├── sacct.py            wraps `sacct` and maps SLURM states -> JobStatus
├── decision.py         determine_action(): submit vs restart vs running vs complete vs error
└── templates/          shell scripts scaffolded by `job-queue init` (see below)
    ├── sbatch_wrapper.sh
    ├── submit.sh
    ├── restart.sh
    ├── job_runner.sh
    └── checkpoint.sh
```

## CLI reference

```bash
job-queue status --metadata PATH [--job-name NAME] [--job-id ID]
# Prints exactly one token to stdout: submit | restart | running | complete | error
# Diagnostics (job id, error detail) go to stderr.

job-queue record-submission --metadata PATH --slurm-job-id ID --script submit.sh|restart.sh
# Called right after `sbatch --parsable ...` succeeds.

job-queue record-checkpoint --metadata PATH --checkpoint-path PATH
# Called by checkpoint.sh before a job exits.

job-queue get-checkpoint --metadata PATH
# Prints the job's last-recorded checkpoint path, or an empty line if none.

job-queue list [--status STATUS]
# Lists jobs from the shared queue db - handy for a quick dashboard view.

job-queue init [--dest DIR] [--force]
# Scaffolds the bundled shell templates into DIR (default: cwd).
```

## Setting up a new job

```bash
mkdir my_job && cd my_job
job-queue init                 # writes sbatch_wrapper.sh, submit.sh, restart.sh,
                                # job_runner.sh, checkpoint.sh into this directory
# edit checkpoint.sh and set REAL_WORK_CMD in submit.sh/restart.sh for your workload
sbatch sbatch_wrapper.sh       # or wire the wrapper's logic into your own sbatch script
```

`job-queue init` only ever touches the small, project-specific shell
scripts - the actual Python logic stays centrally installed, so a fix or
feature added to the package is picked up by every project without
re-copying anything.

## How the pieces fit together

1. **`determine_action()`** (in `decision.py`) is the single source of
   truth for submit/restart/running/complete/error. It loads (or creates)
   a job's metadata file, checks whether a `slurm_job_id` is on record,
   and if so asks `sacct` for ground truth rather than trusting a
   possibly-stale file.

2. **`MetadataStore`** is one JSON file per job (not one shared file), so
   concurrent SLURM jobs never contend for a lock. If no `job_id` is
   supplied, a UUID is generated the first time the file is created and
   persisted, so it's independent of whatever SLURM assigns as its own
   job id and stays stable across every submit/restart cycle.

3. **`QueueDB`** is a SQLite database at
   `~/.local/share/job-queue/queue.db` (override with `$JOB_QUEUE_HOME`),
   in WAL mode so concurrent jobs can write safely. It's a secondary
   index across jobs for dashboards (`job-queue list`) - each job's
   metadata file remains the source of truth for that job's own details.

4. **Checkpointing**: `sbatch_wrapper.sh` only runs once, before the job
   starts, to decide submit vs. restart - it can't catch a signal
   mid-run because it isn't the process SLURM signals. That's why
   `job_runner.sh` - the script that actually gets `sbatch`-ed - traps
   both `SIGTERM` and `EXIT` to run `checkpoint.sh` exactly once,
   covering SLURM time-limit warnings, `scancel`, and ordinary
   successful/erroring completion through one code path.
   `checkpoint.sh` calls `job-queue record-checkpoint`, and `restart.sh`
   reads it back via `job-queue get-checkpoint` - that's the loop that
   lets a restarted job resume instead of starting over.

## Extending it

- **Custom retry policy**: `max_attempts` lives per-job in the metadata
  file, so different job types can have different retry budgets without
  touching `decision.py`.
- **Dashboards**: `job-queue list --status FAILED`, or import
  `QueueDB` directly for anything richer than the CLI's plain-text table.
- **Testing without a cluster**: `sacct.query_sacct()` degrades to
  `UNKNOWN` (falling back to the metadata file's last-recorded status)
  whenever `sacct` isn't on `PATH` - this is what lets the whole pipeline
  be tested without a real SLURM install, exactly as done during
  development of this package.
