#!/bin/bash
#SBATCH --job-name=jq_t3_exhaust_retries
#SBATCH --time=00:02:00
#SBATCH --mem=256mb
#SBATCH --ntasks=1
#SBATCH --output=jq_t3_exhaust_retries-%j.out

# WHAT THIS TESTS: permanent failure - submit()/restart() always fail,
# so repeated invocations climb attempt 1, 2, 3... until max_attempts
# (default 5) is hit, at which point job_queue_run refuses to run
# anything further and exits 1 with the recorded error.
#
# HOW TO RUN (submit 6 times in a row, waiting for each to finish):
#   for i in 1 2 3 4 5 6; do sbatch --wait t3_exhaust_retries.sh; done
#
# TIP: to see the "error" state without 6 real submissions, lower
# max_attempts directly in the metadata file after the FIRST run:
#   python3 -c "
#   import json
#   p = 'metadata/jq_t3_exhaust_retries.json'
#   d = json.load(open(p)); d['max_attempts'] = 2; json.dump(d, open(p,'w'))
#   "
#   Then just 2 more submissions reach the error state.
#
# EXPECT:
#   Runs 1..max_attempts: restart() (after the 1st) runs and fails,
#     recorded FAILED, attempt count increments each time.
#   Run max_attempts+1: job_queue_run prints
#     "Job jq_t3_exhaust_retries failed permanently:" and the exhausted-
#     attempts detail to stderr, exits 1, WITHOUT calling restart().

submit() {
  echo "t3: submit is intentionally failing"
  return 1
}

restart() {
  echo "t3: restart is intentionally failing too"
  return 1
}

. ~/hpclib/hpclib.sh
job_queue_run
