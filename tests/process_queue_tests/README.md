# job_queue tests

Prereqs:
    pip install -e hpclib/job_queue
    python -m unittest discover -s tests/job_queue -p 'test_batch.py' -v

The batch/config-segmentation tests (`test_batch.py`) are pure Python -
no SLURM required.

The rest are LIVE integration tests against a **real SLURM cluster**.
Nothing is stubbed: `sbatch`, `squeue`, and `sacct` must actually work
from wherever you run these, and every test job is a genuine (tiny,
short-lived) SLURM submission. Supply whatever your cluster requires
via environment variables before running:

    TEST_SBATCH_ARGS="--partition=debug --account=myproj" \
    TEST_SBATCH_TIME=00:03:00 \
    TEST_SBATCH_MEM=100M \
      bash tests/job_queue/test_process_queue_live.sh

    TEST_SBATCH_ARGS="--partition=debug --account=myproj" \
      bash tests/job_queue/test_job_queue_run_live.sh

- `test_process_queue_live.sh`: fresh submission, skip-if-really-running
  and skip-if-really-complete, config segmentation under real
  submissions, a real fail -> restart -> complete cycle, and two truly
  concurrent invocations submitting the same job exactly once (verified
  against `squeue`/`sacct`, not just local logs).
- `test_job_queue_run_live.sh`: submit() vs restart() dispatch across
  two real allocations, the running-job short-circuit against a job
  actually observed in state `R`, and checkpoint-on-SIGTERM triggered
  by a real `scancel`.

Each script tracks every job id it submits and `scancel`s any that are
still active on exit (including on failure), so a broken test run
doesn't leave jobs behind on the cluster. Expect these to take several
minutes; timeouts are generous but configurable in
`slurm_test_helpers.sh` if your scheduler is slower to queue/account.

## Example `process_sbatch.sh` (for manual testing/reference)

```bash
#!/bin/bash
#SBATCH --time=0-8:00:00 --mem=10gb --ntasks=1 --signal=B:TERM@60

CONFIG_FILE="$1"

function submit {
  python train.py --config "$CONFIG_FILE"
}

function restart {
  local checkpoint
  checkpoint=$(job-queue get-checkpoint --metadata "$METADATA")
  if [ -n "$checkpoint" ]; then
    python train.py --config "$CONFIG_FILE" --resume-from "$checkpoint"
  else
    python train.py --config "$CONFIG_FILE"
  fi
}

function checkpoint {
  job-queue record-checkpoint --metadata "$METADATA" --checkpoint-path "/scratch/${JOB_NAME}/ckpt_latest.pt"
}

. ~/hpclib/hpclib.sh
job_queue_run
```

Run a batch against it with:

```bash
job_process_queue -s process_sbatch.sh sweep.json
```
