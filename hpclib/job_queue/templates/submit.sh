#!/bin/bash
# submit.sh - fresh submission branch, invoked via `source submit.sh`.
# Expects METADATA and JOB_NAME to already be set by the caller (see
# sbatch_wrapper.sh). Submits job_runner.sh (which wraps the real work
# command with checkpoint-on-exit handling) and records the resulting
# SLURM job id via the installed job-queue CLI.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export REAL_WORK_CMD="${REAL_WORK_CMD:-python train.py}"  # your actual compute command
export CHECKPOINT_SCRIPT="${CHECKPOINT_SCRIPT:-${SCRIPT_DIR}/checkpoint.sh}"
export METADATA JOB_NAME   # job_runner.sh/checkpoint.sh need these at runtime

new_id=$(sbatch --parsable --job-name="${JOB_NAME}" "${SCRIPT_DIR}/job_runner.sh")
echo "Submitted ${JOB_NAME} as SLURM job ${new_id}"

job-queue record-submission --metadata "${METADATA}" --slurm-job-id "${new_id}" --script submit.sh
