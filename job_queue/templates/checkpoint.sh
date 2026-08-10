#!/bin/bash
# checkpoint.sh
#
# Runs before the job ends - whether triggered by SIGTERM (time limit,
# preemption, or `scancel`) or by the work command exiting on its own,
# successfully or not. Put your actual "save state so we can resume"
# logic here, then record where you saved it so restart.sh can find it.
#
# Env vars available (exported by job_runner.sh):
#   JOB_NAME, METADATA
set -uo pipefail

CKPT_DIR="${CKPT_DIR:-/scratch/${JOB_NAME:-job}}"
CKPT_PATH="${CKPT_DIR}/ckpt_latest.pt"

mkdir -p "${CKPT_DIR}"

# --- your real save-state logic goes here -------------------------------
# e.g. copy the most recent checkpoint your training loop already wrote,
# flush a database, snapshot a work directory, etc.
echo "checkpoint saved at $(date -u +%FT%TZ)" >> "${CKPT_DIR}/checkpoint.log"
# -------------------------------------------------------------------------

if [[ -n "${METADATA:-}" ]]; then
    job-queue record-checkpoint --metadata "${METADATA}" --checkpoint-path "${CKPT_PATH}"
fi
