#!/bin/bash
#SBATCH --job-name=jq_t4_checkpoint_sigterm
#SBATCH --time=00:00:30
#SBATCH --signal=B:TERM@15
#SBATCH --mem=256mb
#SBATCH --ntasks=1
#SBATCH --output=jq_t4_checkpoint_sigterm-%j.out

# WHAT THIS TESTS: the SIGTERM -> checkpoint() -> FAILED(143) -> restart()
# path. --time=00:00:30 with --signal=B:TERM@15 makes SLURM send
# SIGTERM ~15s before the 30s time limit (i.e. around t=15s), which
# job_queue_run's trap should catch: kill submit()'s background pid,
# run checkpoint() (must finish within JOB_QUEUE_CHECKPOINT_TIMEOUT,
# default 45s), then record FAILED with exit 143. STOP_AT below is set
# high enough that the 1st run can't reach it before that SIGTERM, but
# low enough that the 2nd run (resuming from the checkpoint) reaches it
# and completes normally well inside another 30s.
#
# HOW TO RUN:
#   sbatch t4_checkpoint_sigterm.sh   # 1st: gets SIGTERM'd, checkpoints
#   sbatch t4_checkpoint_sigterm.sh   # 2nd: restart() resumes and finishes
#
# EXPECT:
#   1st run: counts up in progress.txt once per second; should get cut
#     off by SIGTERM well before reaching STOP_AT. progress.txt stops
#     part-way through, and `job-queue get-checkpoint --metadata
#     metadata/jq_t4_checkpoint_sigterm.json` should print a path
#     afterward. job-queue list shows FAILED, exit_code 143.
#   2nd run: "t4: resuming from checkpoint at count=<N>" where
#     0 < N < STOP_AT, then "t4: reached STOP_AT=... finishing" and
#     job-queue list shows COMPLETED.

STOP_AT=12
WORKDIR="${TMPDIR:-/tmp}/jq_t4_work"
PROGRESS_FILE="$WORKDIR/progress.txt"
mkdir -p "$WORKDIR"

_count_up() {
  local start="$1"
  local i="$start"
  while [ "$i" -lt "$STOP_AT" ]; do
    echo "$i" > "$PROGRESS_FILE"
    echo "t4: count=$i"
    i=$((i+1))
    sleep 1
  done
  echo "t4: reached STOP_AT=$STOP_AT, finishing"
}

submit() {
  echo "t4: starting fresh"
  _count_up 0
}

restart() {
  local ckpt resume_from=0
  ckpt=$(job-queue get-checkpoint --metadata "$METADATA")
  if [ -n "$ckpt" ] && [ -f "$ckpt" ]; then
    resume_from=$(cat "$ckpt")
  fi
  echo "t4: resuming from checkpoint at count=$resume_from"
  _count_up "$resume_from"
}

checkpoint() {
  local ckpt_path="$WORKDIR/checkpoint.txt"
  if [ -f "$PROGRESS_FILE" ]; then
    cp "$PROGRESS_FILE" "$ckpt_path"
    job-queue record-checkpoint --metadata "$METADATA" --checkpoint-path "$ckpt_path"
    echo "t4: checkpointed at $(cat "$ckpt_path")"
  fi
}

. ~/hpclib/hpclib.sh
job_queue_run
