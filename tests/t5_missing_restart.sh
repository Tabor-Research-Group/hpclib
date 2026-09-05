#!/bin/bash
#SBATCH --job-name=jq_t5_missing_restart
#SBATCH --time=00:02:00
#SBATCH --mem=256mb
#SBATCH --ntasks=1
#SBATCH --output=jq_t5_missing_restart-%j.out

# WHAT THIS TESTS: submit() fails, but NO restart() function or
# restart.sh file exists. Per spec, the 2nd invocation (which needs to
# restart) should just terminate cleanly with a clear message instead
# of trying to run something that doesn't exist.
#
# HOW TO RUN:
#   sbatch t5_missing_restart.sh   # 1st: submit() runs, fails
#   sbatch t5_missing_restart.sh   # 2nd: no restart() -> refuses, exit 1
#
# EXPECT:
#   1st run: "t5: submit is intentionally failing", recorded FAILED.
#   2nd run: "No restart() function or restart.sh found for
#     jq_t5_missing_restart; cannot proceed." on stderr, exit 1 -
#     WITHOUT incrementing the attempt count or touching sacct.

submit() {
  echo "t5: submit is intentionally failing"
  return 1
}

# restart() deliberately NOT defined, and no restart.sh sits next to
# this script - that absence is the point of this test.

. ~/hpclib/hpclib.sh
job_queue_run
