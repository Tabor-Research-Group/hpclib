#!/bin/bash
# job_runner.sh
#
# This is the script that actually gets `sbatch`-ed (submit.sh/restart.sh
# point WORK_SCRIPT at this, not directly at your compute command). It
# wraps your real work in a background process so that a trapped signal
# can interrupt `wait` immediately, then runs checkpoint.sh before the
# job actually exits - covering both:
#   - SIGTERM from SLURM (time limit approaching, preemption, `scancel`)
#   - normal exit, successful or not (via the EXIT trap)
# so checkpoint.sh always gets a chance to run exactly once.
#
# --signal tells SLURM to send SIGTERM this many seconds before it would
# otherwise kill the job outright (time limit) or before SIGKILL follows
# a `scancel` (subject to your cluster's KillWait). Raise this if
# checkpoint.sh needs more time to finish.
#SBATCH --signal=B:TERM@60

set -uo pipefail   # intentionally no -e: exit path is controlled by traps below

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKPOINT_SCRIPT="${CHECKPOINT_SCRIPT:-${SCRIPT_DIR}/checkpoint.sh}"
CHECKPOINT_TIMEOUT="${CHECKPOINT_TIMEOUT:-45}"   # must stay under the --signal warning window above
CHECKPOINT_DONE=0

run_checkpoint() {
    if [[ "${CHECKPOINT_DONE}" -eq 1 ]]; then
        return  # TERM and EXIT traps can both fire; only run once
    fi
    CHECKPOINT_DONE=1
    if [[ -f "${CHECKPOINT_SCRIPT}" ]]; then
        echo "[job_runner] running ${CHECKPOINT_SCRIPT}"
        timeout "${CHECKPOINT_TIMEOUT}" bash "${CHECKPOINT_SCRIPT}"
        ckpt_status=$?
        if [[ "${ckpt_status}" -ne 0 ]]; then
            echo "[job_runner] checkpoint.sh exited with status ${ckpt_status}" >&2
        fi
    fi
}

on_term() {
    echo "[job_runner] received SIGTERM - stopping work and checkpointing"
    # Give the actual work process a chance to shut down cleanly first
    # (it may have its own SIGTERM handling), then checkpoint regardless.
    kill -TERM "${work_pid}" 2>/dev/null || true
    wait "${work_pid}" 2>/dev/null
    run_checkpoint
    exit 143   # conventional 128+SIGTERM exit code
}

trap on_term TERM
trap run_checkpoint EXIT

# Run in the background so `wait` can be interrupted by the trap the
# instant SIGTERM arrives, rather than blocking until completion.
${REAL_WORK_CMD} &
work_pid=$!
wait "${work_pid}"
work_status=$?

exit "${work_status}"
