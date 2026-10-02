#!/usr/bin/env python3
"""
Step 2 of the ORCA scan demo without an LLM: the same calls the MCP
tools make, as a script. Pushes a generated scan to the cluster, submits
it as one `orca` job array read from its scan_info.json, waits, and
pulls the outputs back next to the inputs.

    export HPC_REST_URL=http://127.0.0.1:5050
    export HPC_REST_TOKEN_FILE=~/.config/hpclib/llm_token
    python run_scan.py ~/Desktop/sample_scan --remote-dir scans/sample_scan

Standard library only (plus hpclib's rest_client). Safe to rerun: the
upload skips files already there, the submission is idempotent, and the
download skips outputs you already have. `--retry-failed` resubmits only
the points whose tasks failed.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "servers"))
from rest_client import FileSync, RESTClient, RESTClientError  # noqa: E402


def submit(client, args, select=None, key_suffix=""):
    tasks_from = {"path": f"{args.remote_dir}/scan_info.json", "key": "steps", "fields": {"input": "file"}}
    if select is not None:
        tasks_from["select"] = select
    request = dict(
        template="orca",
        params={"nprocs": args.nprocs},
        resources={"time": args.time, "mem": args.mem},
        tasks_from=tasks_from,
        throttle=args.throttle,
        label=args.label,
    )
    plan = client.submit_job(**request, dry_run=True)
    print(f"dry run: {plan['array_size']} tasks, throttle {plan['throttle']}; SLURM says: {plan['slurm_message']}")
    if not plan["accepted"]:
        sys.exit("SLURM rejected the dry run; see the message above")
    job = client.submit_job(**request, idempotency_key=f"{args.label}{key_suffix}")
    print(f"{'already submitted as' if job['duplicate'] else 'submitted'} job {job['job_id']}")
    return job


def wait(client, job_id, poll):
    while True:
        status = client.job_status(job_id)
        counts = ", ".join(f"{n} {s.lower()}" for s, n in sorted(status.get("task_counts", {}).items()))
        print(f"[{time.strftime('%H:%M:%S')}] job {job_id}: {status['state']} ({counts})", flush=True)
        if status["terminal"]:
            return client.job_status(job_id, include_tasks=True)
        # the server waits at most 5 minutes per call
        client.wait_job(job_id, timeout=min(poll, 300))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run a ScanManager scan on the cluster through hpclib's REST API")
    parser.add_argument("scan_dir", help="local directory written by generate_scan.py")
    parser.add_argument("--remote-dir", required=True, help="where the scan goes on the cluster (in the token's dirs)")
    parser.add_argument("--label", help="label for the job (default: the scan directory's name)")
    parser.add_argument("--nprocs", type=int, default=4, help="must match generate_scan.py --nprocs")
    parser.add_argument("--mem", default="16G", help="per task; above nprocs x MaxCore")
    parser.add_argument("--time", default="01:00:00", help="per task")
    parser.add_argument("--throttle", type=int, default=4, help="tasks running at once")
    parser.add_argument("--poll", type=int, default=120, help="seconds between status checks")
    parser.add_argument("--retry-failed", metavar="JOB_ID", help="resubmit only the failed tasks of this job")
    parser.add_argument("--no-wait", action="store_true", help="submit and exit")
    args = parser.parse_args(argv)
    args.scan_dir = os.path.realpath(os.path.expanduser(args.scan_dir))
    args.label = args.label or os.path.basename(args.scan_dir)

    client = RESTClient.from_env()
    # the local side of FileSync: only this scan's directory
    sync = FileSync(client, [args.scan_dir])
    try:
        print(f"connected to {client.health()['hostname']}")
        if args.retry_failed:
            old = client.job_status(args.retry_failed, include_tasks=True)
            failed = [t["source_index"] for t in old["tasks"] if t["index"] in old.get("failed_tasks", [])]
            if not failed:
                sys.exit(f"job {args.retry_failed} has no failed tasks")
            print(f"resubmitting scan points {failed}")
            job = submit(client, args, select=failed, key_suffix=f"-retry-{args.retry_failed}")
        else:
            pushed = sync.push(args.scan_dir, args.remote_dir, pattern="*.inp,scan_info.json")
            print(f"uploaded {len(pushed['uploaded'])} files ({len(pushed['skipped'])} already there)")
            job = submit(client, args)
        if args.no_wait:
            return
        final = wait(client, job["job_id"], args.poll)
        for task in final["tasks"]:
            if task["state"] != "COMPLETED":
                print(f"  point {task['source_index']} ({os.path.basename(task['params']['input'])}): "
                      f"{task['state']}; SLURM log {task['output']}")
        pulled = sync.pull(args.remote_dir, args.scan_dir, pattern="*.out,*.xyz")
        print(f"downloaded {len(pulled['downloaded'])} files ({len(pulled['skipped'])} already here)")
        if final.get("failed_tasks"):
            print(f"{len(final['failed_tasks'])} points failed; rerun them with --retry-failed {job['job_id']}")
    except RESTClientError as e:
        sys.exit(f"error: {e}" + (f"\n{e.payload}" if e.payload and len(e.payload) > 1 else ""))


if __name__ == "__main__":
    main()
