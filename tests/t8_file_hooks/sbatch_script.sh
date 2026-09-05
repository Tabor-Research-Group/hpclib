#!/bin/bash
#SBATCH --job-name=jq_t8_file_hooks
#SBATCH --time=00:02:00
#SBATCH --mem=256mb
#SBATCH --ntasks=1
#SBATCH --output=jq_t8_file_hooks-%j.out

# WHAT THIS TESTS: the file-based hook fallback - this script defines
# NO inline submit()/restart()/failed() functions at all. It relies on
# sibling submit.sh (which defines a matching function) and restart.sh
# (which just runs its work directly at top level, old-template style)
# sitting next to it. Exercises _jq_dispatch's two different fallback
# shapes in one test.
#
# HOW TO RUN (from inside this directory):
#   sbatch t8_file_hooks/sbatch_script.sh   # 1st: submit.sh's submit() fails
#   sbatch t8_file_hooks/sbatch_script.sh   # 2nd: restart.sh runs inline, succeeds
#
# EXPECT:
#   1st run: "t8: submit.sh's submit() failing", recorded FAILED.
#   2nd run: "t8: restart.sh running inline, no function needed",
#     recorded COMPLETED - proving a hook file that just runs code at
#     the top level (rather than defining a same-named function) is
#     honored too.

. ~/hpclib/hpclib.sh
job_queue_run
