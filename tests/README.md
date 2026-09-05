# job_queue_run stress tests

Eight small sbatch scripts, each isolating one path through
`job_queue_run` (in `lib/job_queue.sh`). Run them from a scratch
directory on a real cluster - they submit real (tiny, short-lived)
SLURM jobs.

## Setup

```bash
mkdir -p ~/jq-stress-tests && cd ~/jq-stress-tests
# copy t1..t8 and reset.sh here
chmod +x *.sh reset.sh t8_file_hooks/*.sh
```

Requires `~/hpclib` (or `$HPCLIB_DIR`) present, and `job-queue`
installed on `PATH` (`pip install -e /path/to/job-queue-pkg`).

## Running

Each script's header comment says exactly how to invoke it and what
to expect. In short:

| Script | Exercises | Invocations needed |
|---|---|---|
| `t1_success.sh` | happy path + idempotent re-run | 2 |
| `t2_fail_then_restart.sh` | FAILED -> restart() -> COMPLETED | 2 |
| `t3_exhaust_retries.sh` | permanent failure -> `error` | up to `max_attempts`+1 |
| `t4_checkpoint_sigterm.sh` | SIGTERM -> checkpoint() -> restart() resumes | 2 |
| `t5_missing_restart.sh` | restart needed but undefined -> clean terminate | 2 |
| `t6_missing_submit.sh` | submit undefined at all -> immediate fail | 1 |
| `t7_duplicate_submission.sh` | concurrent resubmission -> `running` guard | 2 (parallel) |
| `t8_file_hooks/sbatch_script.sh` | hooks as sibling files, both styles | 2 |

Between full passes, clear state:

```bash
./reset.sh
```

## What to watch for

- `job-queue list` after each step - status should always match what
  the script's header comment says.
- Nothing should ever print a Python traceback; every failure path
  above is a deliberate, clean `exit 1` with a one-line message.
- `t3`, `t4`, and `t7` are the ones actually worth watching closely -
  they're the cases where timing (attempt exhaustion, real SIGTERM
  delivery, real concurrent scheduling) rather than pure logic is
  being exercised.
