"""
Command-line interface for job_queue, exposed as the `job-queue` console
script (and `python -m job_queue`) once this package is installed.

Subcommands:
    status              decide submit/restart/running/complete/error for a job
    record-submission   persist a new SLURM job id after `sbatch --parsable`
    record-checkpoint   persist a checkpoint path from checkpoint.sh
    get-checkpoint       print a job's last-recorded checkpoint path (or "")
    list                list jobs from the shared queue db, optionally by status
    init                scaffold the bundled shell templates into a directory
"""
import argparse
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Optional, Sequence

from .decision import determine_action
from .metadata import MetadataStore
from .queue_db import QueueDB, QueueUnavailable
from .models import JobStatus


def _cmd_status(args: argparse.Namespace) -> int:
    queue = QueueDB()
    decision = determine_action(
        metadata_path=args.metadata,
        job_name=args.job_name,
        job_id=args.job_id,
        queue=queue,
    )
    # STDOUT: exactly one token, meant for `status=$(job-queue status ...)`
    print(decision.action)
    # STDERR: diagnostics, kept off the captured value
    if decision.job_id:
        print(f"job_id={decision.job_id}", file=sys.stderr)
    if decision.detail:
        print(decision.detail, file=sys.stderr)
    return 0


def _cmd_record_submission(args: argparse.Namespace) -> int:
    store = MetadataStore(args.metadata)
    meta = store.record_submission(slurm_job_id=args.slurm_job_id, script=args.script)

    queue = QueueDB()
    queue.upsert_job(
        job_id=meta.job_id,
        job_name=meta.job_name,
        metadata_path=str(Path(args.metadata).resolve()),
        status="PENDING",
        slurm_job_id=args.slurm_job_id,
    )
    queue.mark_submitted(job_id=meta.job_id, slurm_job_id=args.slurm_job_id)

    print(f"Recorded submission: job_id={meta.job_id} slurm_job_id={args.slurm_job_id}")
    return 0


def _cmd_record_checkpoint(args: argparse.Namespace) -> int:
    store = MetadataStore(args.metadata)
    if not store.exists():
        print(f"No metadata file at {args.metadata}; nothing to record against", file=sys.stderr)
        return 1
    store.update(checkpoint_path=args.checkpoint_path)
    print(f"Recorded checkpoint path: {args.checkpoint_path}")
    return 0


def _cmd_get_checkpoint(args: argparse.Namespace) -> int:
    store = MetadataStore(args.metadata)
    meta = store.load()
    # Print exactly one line (possibly empty) for `checkpoint=$(job-queue get-checkpoint ...)`
    print(meta.checkpoint_path or "" if meta else "")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    queue = QueueDB()
    try:
        rows = queue.list_jobs(status=args.status)
    except QueueUnavailable as exc:
        print(f"job-queue list: {exc}", file=sys.stderr)
        print("Try again in a moment - the shared queue db is briefly locked.", file=sys.stderr)
        return 1
    if not rows:
        print("No jobs found." if not args.status else f"No jobs with status {args.status}.")
        return 0
    widths = {"job_name": 24, "status": 11, "slurm_job_id": 12, "job_id": 36}
    header = f"{'JOB_NAME':<{widths['job_name']}} {'STATUS':<{widths['status']}} {'SLURM_ID':<{widths['slurm_job_id']}} JOB_ID"
    print(header)
    for row in rows:
        print(
            f"{row['job_name']:<{widths['job_name']}} "
            f"{row['status']:<{widths['status']}} "
            f"{str(row['slurm_job_id'] or ''):<{widths['slurm_job_id']}} "
            f"{row['job_id']}"
        )
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.dest)
    dest.mkdir(parents=True, exist_ok=True)

    template_dir = resources.files("job_queue").joinpath("templates")
    copied = []
    for entry in template_dir.iterdir():
        if not entry.is_file():
            continue
        target = dest / entry.name
        if target.exists() and not args.force:
            print(f"skip (exists): {target}", file=sys.stderr)
            continue
        with resources.as_file(entry) as src_path:
            shutil.copy(src_path, target)
        target.chmod(target.stat().st_mode | 0o111)  # keep scripts executable
        copied.append(target)

    for path in copied:
        print(f"wrote {path}")
    if not copied:
        print("Nothing copied (use --force to overwrite existing files).", file=sys.stderr)
    return 0

def _cmd_record_result(args: argparse.Namespace) -> int:
    store = MetadataStore(args.metadata)
    if not store.exists():
        print(f"No metadata file at {args.metadata}; nothing to record against", file=sys.stderr)
        return 1
    status = JobStatus.COMPLETED if args.status == "completed" else JobStatus.FAILED
    meta = store.record_result(status=status, exit_code=args.exit_code, error_message=args.error_message)

    queue = QueueDB()
    queue.update_status(meta.job_id, status.value)

    print(f"Recorded result: job_id={meta.job_id} status={status.value} exit_code={args.exit_code}")
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-queue", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="decide submit/restart/complete/error for a job")
    p_status.add_argument("--metadata", required=True)
    p_status.add_argument("--job-name", default=None)
    p_status.add_argument("--job-id", default=None)
    p_status.set_defaults(func=_cmd_status)

    p_sub = sub.add_parser("record-submission", help="record a new SLURM job id after sbatch")
    p_sub.add_argument("--metadata", required=True)
    p_sub.add_argument("--slurm-job-id", required=True, type=int)
    p_sub.add_argument("--script", required=True, choices=["submit.sh", "restart.sh"])
    p_sub.set_defaults(func=_cmd_record_submission)

    p_ckpt = sub.add_parser("record-checkpoint", help="record a checkpoint path")
    p_ckpt.add_argument("--metadata", required=True)
    p_ckpt.add_argument("--checkpoint-path", required=True)
    p_ckpt.set_defaults(func=_cmd_record_checkpoint)

    p_getckpt = sub.add_parser("get-checkpoint", help="print a job's last checkpoint path")
    p_getckpt.add_argument("--metadata", required=True)
    p_getckpt.set_defaults(func=_cmd_get_checkpoint)

    p_list = sub.add_parser("list", help="list jobs from the shared queue db")
    p_list.add_argument("--status", default=None, help="filter by status, e.g. FAILED")
    p_list.set_defaults(func=_cmd_list)

    p_init = sub.add_parser("init", help="scaffold the bundled shell templates into a directory")
    p_init.add_argument("--dest", default=".", help="destination directory (default: cwd)")
    p_init.add_argument("--force", action="store_true", help="overwrite existing files")
    p_init.set_defaults(func=_cmd_init)

    p_result = sub.add_parser("record-result", help="record a job's final outcome directly (no sacct polling)")
    p_result.add_argument("--metadata", required=True)
    p_result.add_argument("--status", required=True, choices=["completed", "failed"])
    p_result.add_argument("--exit-code", type=int, default=None)
    p_result.add_argument("--error-message", default=None)
    p_result.set_defaults(func=_cmd_record_result)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
