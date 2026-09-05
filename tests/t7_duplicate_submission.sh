#!/bin/bash
#SBATCH --job-name=jq_t7_duplicate_submission
#SBATCH --time=00:00:30
#SBATCH --mem=256mb
#SBATCH --ntasks=1
#SBATCH --output=jq_t7_duplicate_submission-%j.out

# WHAT THIS TESTS: submitting the SAME script twice back-to-back
# before the first has finished (or even started, if the queue is
# busy). Since both invocations share the same METADATA path (derived
# from this script's own directory name), the 2nd `sbatch` should see
# status "running" and do nothing, rather than launching a second
# concurrent attempt against the same metadata file.
#
# HOW TO RUN (fire both quickly, from the same directory):
#   sbatch t7_duplicate_submission.sh &
#   sbatch t7_duplicate_submission.sh &
#   wait
#
# EXPECT:
#   Exactly ONE of the two runs prints "t7: doing the real work" and
#   "Job jq_t7_duplicate_submission completed."
#   The OTHER prints "Job jq_t7_duplicate_submission is already
#   active; nothing to do." and exits 0 immediately, without ever
#   calling submit(). Which one "wins" depends on scheduling, but
#   there should never be two overlapping "doing the real work" lines
#   in the two .out files.

submit() {
  echo "t7: doing the real work"
  sleep 10
}

. ~/hpclib/hpclib.sh
job_queue_run
