#!/bin/bash
# restart.sh - resume-after-failure branch, invoked via `source restart.sh`.
# Reads back checkpoint_path from metadata (written by checkpoint.sh on
# the previous attempt) via the job-queue CLI and folds it into the work
# command so the job resumes instead of starting over.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CHECKPOINT_SCRIPT="${CHECKPOINT_SCRIPT:-${SCRIPT_DIR}/checkpoint.sh}"
export METADATA JOB_NAME

checkpoint=$(job-queue get-checkpoint --metadata "${METADATA}")

base_cmd="${REAL_WORK_CMD:-python train.py}"
if [[ -n "$checkpoint" ]]; then
    export REAL_WORK_CMD="${base_cmd} --resume-from ${checkpoint}"
    echo "Resuming ${JOB_NAME} from checkpoint: ${checkpoint}"
else
    export REAL_WORK_CMD="${base_cmd}"
    echo "No checkpoint on record for ${JOB_NAME}; restarting from scratch"
fi

new_id=$(sbatch --parsable --job-name="${JOB_NAME}" "${SCRIPT_DIR}/job_runner.sh")
echo "Restarted ${JOB_NAME} as SLURM job ${new_id}"

job-queue record-submission --metadata "${METADATA}" --slurm-job-id "${new_id}" --script restart.sh
