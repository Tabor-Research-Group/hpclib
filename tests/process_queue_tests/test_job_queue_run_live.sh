#!/bin/bash
# LIVE (real-SLURM) tests for job_queue_run itself - the COMPUTE-NODE
# dispatch: submit() vs restart() selection, the running/complete
# short-circuit against a job that's genuinely still active, and
# checkpoint-on-SIGTERM against a real `scancel`.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT=$SCRATCH
cd $REPO_ROOT
. "$SCRIPT_DIR/harness.sh"
. "$SCRIPT_DIR/slurm_test_helpers.sh"

export HPCLIB_DIR="$REPO_ROOT/hpclib"
. "$HPCLIB_DIR/hpclib.sh"

command -v job_queue >/dev/null 2>&1 || { echo "job_queue not on PATH - pip install -e hpclib/job_queue" >&2; exit 1; }

WORKDIR=$(mktemp -d)
WORKDIR="$SCRIPT_DIR$WORKDIR"
mkdir -p $WORKDIR
trap 'cleanup_tracked_jobs; rm -rf "$WORKDIR"' EXIT

TEST_SBATCH_ARGS="--ntasks=1"
TEST_SBATCH_TIME=00:03:00
TEST_SBATCH_MEM=100M

############################################################################
test_case "fresh job on a real allocation: submit() runs, records COMPLETED"
############################################################################
d="$WORKDIR/t1"; mkdir -p "$d"
job_name="$(unique_job_name fresh)"
write_process_sbatch "$d/process_sbatch.sh" 'touch "'"$d"'/submit-ran"; exit 0' 'touch "'"$d"'/restart-ran"; exit 0' "$HPCLIB_DIR"

echo "parsing job id"
id=$(sbatch --parsable --job-name="$job_name" \
       --export="ALL,JOB_NAME=$job_name,METADATA=$d/metadata.json" \
       "$d/process_sbatch.sh" "/dev/null" | cut -d';' -f1)
echo "got job $id"
track_job_id "$id"

state=$(wait_for_terminal_state "$id" 180)
assert_eq "$state" "COMPLETED" "the real job completed"
assert_file_exists "$d/submit-ran" "submit() actually ran on the compute node"
[ -f "$d/restart-ran" ] && fail "restart() should NOT have run" || pass "restart() did not run"
recorded=$(python3 -c "import json;print(json.load(open('$d/metadata.json'))['status'])")
assert_eq "$recorded" "COMPLETED" "metadata records COMPLETED after the real run"


############################################################################
test_case "previously-failed job, attempts remaining: restart() runs on the next real allocation"
############################################################################
d="$WORKDIR/t2"; mkdir -p "$d"
job_name="$(unique_job_name flaky)"
write_process_sbatch "$d/process_sbatch.sh" \
  'touch "'"$d"'/submit-ran"; exit 1' \
  'touch "'"$d"'/restart-ran"; exit 0' \
  "$HPCLIB_DIR"

id1=$(sbatch --parsable --job-name="$job_name" \
        --export="ALL,JOB_NAME=$job_name,METADATA=$d/metadata.json" \
       "$d/process_sbatch.sh" "/dev/null" | cut -d';' -f1)
track_job_id "$id1"
state1=$(wait_for_terminal_state "$id1" 180)
assert_eq "$state1" "FAILED" "first real attempt genuinely failed"
assert_file_exists "$d/submit-ran" "submit() ran on the first attempt"

decision=$(job_queue status --metadata "$d/metadata.json" --job-name "$job_name" 2>/dev/null)
assert_eq "$decision" "restart" "determine_action says restart after the real failure"

id2=$(sbatch --parsable --job-name="$job_name" \
        --export="ALL,JOB_NAME=$job_name,METADATA=$d/metadata.json" \
       "$d/process_sbatch.sh" "/dev/null" | cut -d';' -f1)
track_job_id "$id2"
state2=$(wait_for_terminal_state "$id2" 180)
assert_eq "$state2" "COMPLETED" "restart() completed on the second real allocation"
assert_file_exists "$d/restart-ran" "restart() (not submit()) ran the second time"


############################################################################
test_case "job actually still RUNNING: a second job_queue_run invocation does no work"
############################################################################
d="$WORKDIR/t3"; mkdir -p "$d"
job_name="$(unique_job_name still-running)"
write_process_sbatch "$d/process_sbatch.sh" 'sleep 90' 'exit 0' "$HPCLIB_DIR"

id=$(sbatch --parsable --job-name="$job_name" \
       --export="ALL,JOB_NAME=$job_name,METADATA=$d/metadata.json" \
       "$d/process_sbatch.sh" "/dev/null" | cut -d';' -f1)
track_job_id "$id"
wait_for_squeue_state "$id" "R" 120 || fail "sanity check: job never reached RUNNING before timeout"
wait_for_metadata_recorded "$d/metadata.json" "$id" 60 || fail "sanity check: job_queue_run never recorded its own submission in time"

# Invoke job_queue_run's own dispatch logic locally (as if from a
# second, mistaken hand-invocation), pointed at the SAME metadata file
# a genuinely-running SLURM job currently owns.
JOB_NAME="$job_name" METADATA="$d/metadata.json" bash -c '
  function submit  { touch "'"$d"'/submit-ran";  exit 0; }
  function restart { touch "'"$d"'/restart-ran"; exit 0; }
  . "'"$HPCLIB_DIR"'/hpclib.sh"
  unset SLURM_JOB_ID
  job_queue_run
' >"$d/second_invocation.log" 2>&1
status=$?

[ -f "$d/submit-ran" ]  && fail "submit() must NOT run against a genuinely active job"  || pass "submit() did not run"
[ -f "$d/restart-ran" ] && fail "restart() must NOT run against a genuinely active job" || pass "restart() did not run"
assert_eq "$status" "0" "the redundant invocation exits 0 without doing anything"

scancel "$id" >/dev/null 2>&1   # done observing it; let the real sleep job go


############################################################################
test_case "a real scancel mid-run delivers SIGTERM: checkpoint() runs once, FAILED recorded"
############################################################################
d="$WORKDIR/t4"; mkdir -p "$d"
job_name="$(unique_job_name sigterm)"
write_process_sbatch "$d/process_sbatch.sh" \
  'touch "'"$d"'/submit-started"; sleep 120' \
  'exit 0' \
  "$HPCLIB_DIR"
# checkpoint() is appended manually below the generated submit/restart,
# since write_process_sbatch only templates those two.
python3 - "$d/process_sbatch.sh" <<PY
import re
path = "$d/process_sbatch.sh"
src = open(path).read()
src = src.replace(
    'function restart {',
    'function checkpoint { echo one >> "$d/checkpoint-count"; }\n\nfunction restart {'
)
open(path, "w").write(src)
PY

id=$(sbatch --parsable --job-name="$job_name" \
       --export="ALL,JOB_NAME=$job_name,METADATA=$d/metadata.json" \
       "$d/process_sbatch.sh" "/dev/null" | cut -d';' -f1)
track_job_id "$id"
wait_for_squeue_state "$id" "R" 120 || fail "sanity check: job never reached RUNNING before timeout"
wait_for_metadata_recorded "$d/metadata.json" "$id" 60 || fail "sanity check: job_queue_run never recorded its own submission in time"

for _ in $(seq 1 30); do [ -f "$d/submit-started" ] && break; sleep 1; done
assert_file_exists "$d/submit-started" "submit()'s real background work actually started before scancel"

scancel "$id"
state=$(wait_for_terminal_state "$id" 120)
# `scancel` delivers a real SIGTERM here (not the --signal pre-warning
# path, which only fires ahead of a TIME LIMIT kill) - SLURM's own
# terminal classification is typically CANCELLED; our own bookkeeping
# in _jq_finalize is independent of that (see metadata.record_result's
# docstring) and is what we assert on below.
[ "$state" = "TIMEOUT" ] && fail "job hit its time limit before scancel could act - raise TEST_SBATCH_TIME"
wait_for_metadata_status "$d/metadata.json" 60 >/dev/null || fail "sanity check: job_queue_run never finished recording its own result in time"

lines=$(wc -l < "$d/checkpoint-count" 2>/dev/null || echo 0)
assert_eq "$lines" "1" "checkpoint() ran exactly once under a real SIGTERM (TERM/EXIT traps didn't double-fire)"
recorded=$(python3 -c "import json;print(json.load(open('$d/metadata.json'))['status'])")
assert_eq "$recorded" "FAILED" "a real scancel'd job is recorded FAILED (143) in our own metadata"


harness_summary
exit $?
