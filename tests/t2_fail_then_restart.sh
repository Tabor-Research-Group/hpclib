#!/bin/bash
#SBATCH --job-name=jq_t2_fail_then_restart
#SBATCH --time=00:02:00
#SBATCH --mem=256mb
#SBATCH --ntasks=1
#SBATCH --output=jq_t2_fail_then_restart-%j.out

# WHAT THIS TESTS: submit() fails -> job_queue_run records FAILED with
# attempt=1 (< max_attempts, default 5) -> next invocation asks for
# "restart" instead of "submit" -> restart() runs and succeeds.
#
# HOW TO RUN:
#   sbatch t2_fail_then_restart.sh   # 1st: submit() runs, exits 1
#   sbatch t2_fail_then_restart.sh   # 2nd: restart() runs, exits 0
#
# EXPECT:
#   1st run stdout: "t2: submit is intentionally failing" then
#     "Job jq_t2_fail_then_restart failed (exit 1)." on stderr.
#   2nd run stdout: "t2: restart succeeding" then
#     "Job jq_t2_fail_then_restart completed."
#   `job-queue list` shows FAILED after the 1st run, COMPLETED after
#   the 2nd.

submit() {
  echo "t2: submit is intentionally failing"
  return 1
}

restart() {
  echo "t2: restart succeeding"
}

. ~/hpclib/hpclib.sh
job_queue_run
