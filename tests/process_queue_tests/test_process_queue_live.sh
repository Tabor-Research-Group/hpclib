#!/bin/bash
# LIVE (real-SLURM) tests for job_process_queue - the login-node
# precheck that decides whether to sbatch a job at all. Every job
# submitted here is a real, tiny sbatch job; assertions poll real
# sacct/squeue rather than a stub.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRATCH"
. "$SCRIPT_DIR/harness.sh"
. "$SCRIPT_DIR/slurm_test_helpers.sh"

export HPCLIB_DIR="$REPO_ROOT/hpclib"
. "$HPCLIB_DIR/hpclib.sh"

command -v job_queue >/dev/null 2>&1 || { echo "job_queue not on PATH - pip install -e hpclib/job_queue" >&2; exit 1; }

WORKDIR=$(mktemp -d)
WORKDIR="$SCRIPT_DIR$WORKDIR"
# trap 'cleanup_tracked_jobs; rm -rf "$WORKDIR"' EXIT

function make_batch_file {
  # make_batch_file OUT job_name[:key=val,...] ...
  local out="$1"; shift
  python3 - "$out" "$@" <<'PY'
import json, sys
out, entries = sys.argv[1], []
for spec in sys.argv[2:]:
    name, _, fields = spec.partition(":")
    entry = {"job_name": name}
    for kv in filter(None, fields.split(",")):
        k, v = kv.split("=")
        try: v = float(v) if "." in v else int(v)
        except ValueError: pass
        entry[k] = v
    entries.append(entry)
json.dump(entries, open(out, "w"))
PY
}

TEST_SBATCH_ARGS="--ntasks=1"
TEST_SBATCH_TIME=00:03:00
TEST_SBATCH_MEM=100M


############################################################################
test_case "fresh job: sbatch'd exactly once, config segmentation preserved, runs to COMPLETED"
############################################################################
d="$WORKDIR/t1"; mkdir -p "$d"
job_a="$(unique_job_name fresh-a)"
job_b="$(unique_job_name fresh-b)"
write_process_sbatch "$d/process_sbatch.sh" 'exit 0' 'exit 0' "$HPCLIB_DIR"
make_batch_file "$d/batch.json" "$job_a:lr=0.1" "$job_b:lr=0.2"

job_process_queue -s "$d/process_sbatch.sh" -m "$d/metadata" -c "$d/configs" "$d/batch.json" \
  >"$d/run.log" 2>&1

id_a=$(grep -A2 "^\[$job_a\]" "$d/run.log" | grep -oE 'Submitted batch job [0-9]+' | awk '{print $4}')
id_b=$(grep -A2 "^\[$job_b\]" "$d/run.log" | grep -oE 'Submitted batch job [0-9]+' | awk '{print $4}')
[ -n "$id_a" ] && track_job_id "$id_a"
[ -n "$id_b" ] && track_job_id "$id_b"

assert_eq "$([ -n "$id_a" ] && echo yes || echo no)" "yes" "job-a got a real SLURM job id"
assert_eq "$([ -n "$id_b" ] && echo yes || echo no)" "yes" "job-b got a real SLURM job id"
assert_count "$(grep -c "Submitted batch job" "$d/run.log")" 2 "exactly two sbatch calls total (one per job)"

lr_a=$(python3 -c "import json;print(json.load(open('$d/configs/$job_a/config.json'))['lr'])")
lr_b=$(python3 -c "import json;print(json.load(open('$d/configs/$job_b/config.json'))['lr'])")
assert_eq "$lr_a" "0.1" "job-a's config.json has job-a's own lr, not job-b's"
assert_eq "$lr_b" "0.2" "job-b's config.json has job-b's own lr, not job-a's"

state_a=$(wait_for_terminal_state "$id_a" 180)
state_b=$(wait_for_terminal_state "$id_b" 180)
assert_eq "$state_a" "COMPLETED" "job-a actually completed on the real cluster"
assert_eq "$state_b" "COMPLETED" "job-b actually completed on the real cluster"

# job_queue_run runs inside the job itself, so metadata reaches its
# final state only once sacct/our own poll confirms the job is done.
recorded_a=$(python3 -c "import json;print(json.load(open('$d/metadata/$job_a.json'))['status'])")
assert_eq "$recorded_a" "COMPLETED" "metadata for job-a reflects the real completion"


############################################################################
test_case "re-running while already RUNNING or already COMPLETE: no duplicate sbatch"
############################################################################
d="$WORKDIR/t2"; mkdir -p "$d"
job_long="$(unique_job_name already-running)"
job_done="$(unique_job_name already-done)"
job_new="$(unique_job_name brand-new)"
write_process_sbatch "$d/process_sbatch.sh" 'sleep 60' 'exit 0' "$HPCLIB_DIR"
make_batch_file "$d/batch.json" "$job_long:x=1" "$job_done:x=1" "$job_new:x=1"

# Submit job_long directly (bypassing job_process_queue) so we control
# exactly when it's running, and job_done so we can drive it to a real
# COMPLETED state first.
mkdir -p "$d/metadata" "$d/configs/$job_long" "$d/configs/$job_done"
echo '{}' > "$d/configs/$job_long/config.json"
echo '{}' > "$d/configs/$job_done/config.json"
cp "$d/process_sbatch.sh" "$d/configs/$job_long/process_sbatch.sh"
write_process_sbatch "$d/configs/$job_done/process_sbatch.sh" 'exit 0' 'exit 0' "$HPCLIB_DIR"

id_long=$(sbatch --parsable --job-name="$job_long" --export="ALL,JOB_NAME=$job_long,METADATA=$d/metadata/$job_long.json" \
  "$d/configs/$job_long/process_sbatch.sh" "$d/configs/$job_long/config.json" | cut -d';' -f1)
track_job_id "$id_long"
id_done=$(sbatch --parsable --job-name="$job_done" --export="ALL,JOB_NAME=$job_done,METADATA=$d/metadata/$job_done.json" \
  "$d/configs/$job_done/process_sbatch.sh" "$d/configs/$job_done/config.json" | cut -d';' -f1)
track_job_id "$id_done"

wait_for_squeue_state "$id_long" "R" 120 || fail "sanity check: job_long never reached RUNNING"
wait_for_terminal_state "$id_done" 180 >/dev/null

job_process_queue -s "$d/process_sbatch.sh" -m "$d/metadata" -c "$d/configs" "$d/batch.json" \
  >"$d/run.log" 2>&1

assert_count "$(grep -c "\[$job_long\].*RUNNING.*skipping\|\[$job_long\] running - skipping" "$d/run.log")" 1 \
  "an actually-RUNNING job is skipped, not resubmitted"
assert_count "$(grep -c "\[$job_done\].*complete.*skipping" "$d/run.log")" 1 \
  "an actually-COMPLETED job is skipped, not resubmitted"
id_new=$(grep -A2 "^\[$job_new\]" "$d/run.log" | grep -oE 'Submitted batch job [0-9]+' | awk '{print $4}')
[ -n "$id_new" ] && track_job_id "$id_new"
assert_eq "$([ -n "$id_new" ] && echo yes || echo no)" "yes" "the genuinely new job in the same batch IS submitted"


############################################################################
test_case "job that really fails is genuinely restarted, and the restart really completes"
############################################################################
d="$WORKDIR/t3"; mkdir -p "$d"
job_flaky="$(unique_job_name flaky)"
write_process_sbatch "$d/process_sbatch.sh" 'exit 1' 'exit 0' "$HPCLIB_DIR"
make_batch_file "$d/batch.json" "$job_flaky:x=1"

job_process_queue -s "$d/process_sbatch.sh" -m "$d/metadata" -c "$d/configs" "$d/batch.json" \
  >"$d/run1.log" 2>&1
id1=$(extract_job_id "$d/run1.log")
track_job_id "$id1"
state1=$(wait_for_terminal_state "$id1" 180)
assert_eq "$state1" "FAILED" "the flaky job's first attempt actually failed on the real cluster"

decision=$(job_queue status --metadata "$d/metadata/$job_flaky.json" --job-name "$job_flaky" 2>/dev/null)
assert_eq "$decision" "restart" "determine_action reports 'restart' after a real sacct-confirmed failure"

job_process_queue -s "$d/process_sbatch.sh" -m "$d/metadata" -c "$d/configs" "$d/batch.json" \
  >"$d/run2.log" 2>&1
id2=$(extract_job_id "$d/run2.log")
track_job_id "$id2"
[ "$id1" != "$id2" ] && pass "the restart got a NEW real SLURM job id" || fail "restart reused the same job id ($id1)"

state2=$(wait_for_terminal_state "$id2" 180)
assert_eq "$state2" "COMPLETED" "the restarted attempt actually completed"
recorded=$(python3 -c "import json;print(json.load(open('$d/metadata/$job_flaky.json'))['attempt'])")
assert_eq "$recorded" "2" "metadata's attempt counter reflects both real submissions"


############################################################################
test_case "two truly-concurrent job_process_queue runs submit the same job only once"
############################################################################
# No stubbing of any kind here: two real processes race, on the SAME
# host or not, to make the submit-vs-skip decision for one job. The
# mkdir lock in job_process_queue is what's actually under test.
d="$WORKDIR/t4"; mkdir -p "$d"
job_race="$(unique_job_name race)"
write_process_sbatch "$d/process_sbatch.sh" 'sleep 5' 'exit 0' "$HPCLIB_DIR"
make_batch_file "$d/batch.json" "$job_race:x=1"

job_process_queue -s "$d/process_sbatch.sh" -m "$d/metadata" -c "$d/configs" "$d/batch.json" \
  >"$d/racelog1.log" 2>&1 &
p1=$!
job_process_queue -s "$d/process_sbatch.sh" -m "$d/metadata" -c "$d/configs" "$d/batch.json" \
  >"$d/racelog2.log" 2>&1 &
p2=$!
wait "$p1" "$p2"

submitted=$(grep -h "Submitted batch job" "$d/racelog1.log" "$d/racelog2.log" | wc -l | tr -d ' ')
assert_count "$submitted" 1 "exactly one of the two truly-concurrent runs actually called sbatch"
id_race=$(grep -h -oE 'Submitted batch job [0-9]+' "$d/racelog1.log" "$d/racelog2.log" | awk '{print $4}')
[ -n "$id_race" ] && track_job_id "$id_race"

# Confirm against the SCHEDULER, not just our own logs, that only one
# job with this name is actually known to SLURM.
sleep 2   # let sacct/squeue catch up with the just-submitted job
active_count=$(squeue -n "$job_race" -h | wc -l | tr -d ' ')
history_count=$(sacct --name="$job_race" --noheader --format=JobID | grep -c '^[0-9]\+$')
assert_count "$((active_count>0 ? active_count : history_count))" 1 \
  "SLURM itself shows exactly one job ever named $job_race"


harness_summary
exit $?
