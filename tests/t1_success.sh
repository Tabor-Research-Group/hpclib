#!/bin/bash
#SBATCH --job-name=jq_t1_success
#SBATCH --time=00:02:00
#SBATCH --mem=256mb
#SBATCH --ntasks=1
#SBATCH --output=jq_t1_success-%j.out

# WHAT THIS TESTS: the plain happy path - submit() runs once and
# succeeds, job_queue_run records COMPLETED.
#
# HOW TO RUN:
#   sbatch t1_success.sh          # 1st: submit() runs, exits 0
#   sbatch t1_success.sh          # 2nd: should NOT run submit() again
#
# EXPECT:
#   1st run stdout: "t1: doing the one and only thing" then
#     "Job jq_t1_success completed."
#   2nd run stdout: "Job jq_t1_success has already finished." and a
#     clean (0) exit, with submit() never invoked.
#   `job-queue list` shows jq_t1_success as COMPLETED throughout.

submit() {
  echo "t1: doing the one and only thing"
}

. ~/hpclib/hpclib.sh
job_queue_run
