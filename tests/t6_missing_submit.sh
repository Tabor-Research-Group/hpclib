#!/bin/bash
#SBATCH --job-name=jq_t6_missing_submit
#SBATCH --time=00:02:00
#SBATCH --mem=256mb
#SBATCH --ntasks=1
#SBATCH --output=jq_t6_missing_submit-%j.out

# WHAT THIS TESTS: no submit() function AND no submit.sh file at all -
# the most basic misconfiguration. The very first invocation (status
# will be "submit", since no metadata exists yet) should fail fast
# with a clear message, WITHOUT ever calling job-queue record-submission
# (i.e. no PENDING entry should show up in `job-queue list`).
#
# HOW TO RUN:
#   sbatch t6_missing_submit.sh
#
# EXPECT:
#   "No submit() function or submit.sh found for jq_t6_missing_submit;
#   cannot proceed." on stderr, exit 1. `job-queue list` should show
#   this job's metadata file as freshly created (status NEW, from
#   determine_action's own load_or_create) but never PENDING.

# submit() deliberately NOT defined, and no submit.sh sits next to
# this script - that absence is the point of this test.

. ~/hpclib/hpclib.sh
job_queue_run
