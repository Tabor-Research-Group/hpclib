#!/bin/bash
# Shared helpers for the LIVE (real-SLURM) job_queue integration
# tests. Assumes an actual `sbatch`/`squeue`/`sacct` are on PATH and
# reachable from wherever these tests run - nothing here fakes the
# scheduler.
#
# Cluster-specific requirements (partition, account, QOS, ...) that
# these test jobs need in order to actually schedule are supplied by
# the caller via TEST_SBATCH_ARGS, e.g.:
#
#   TEST_SBATCH_ARGS="--partition=debug --account=myproj" bash test_process_queue_live.sh
set -uo pipefail

TEST_SBATCH_TIME="${TEST_SBATCH_TIME:-00:03:00}"
TEST_SBATCH_MEM="${TEST_SBATCH_MEM:-100M}"
TEST_SBATCH_ARGS="${TEST_SBATCH_ARGS:-}"

for _bin in sbatch squeue sacct; do
  if ! command -v "$_bin" >/dev/null 2>&1; then
    echo "FATAL: '$_bin' not found - these are LIVE tests and require a real SLURM cluster" >&2
    exit 1
  fi
done

# Tracks every job id this test run has submitted, so a failing test
# doesn't leave orphaned jobs behind on the real cluster.
declare -a _LIVE_JOB_IDS=()
function track_job_id { _LIVE_JOB_IDS+=("$1"); }
function cleanup_tracked_jobs {
  local id
  for id in "${_LIVE_JOB_IDS[@]:-}"; do
    [ -n "$id" ] && scancel "$id" >/dev/null 2>&1
  done
}
trap cleanup_tracked_jobs EXIT

# Unique per-run job name prefix, so concurrent test runs (or a rerun
# right after a prior one) never collide on `sacct --name=...` history
# from a previous invocation.
function unique_job_name {
  echo "jqtest-$1-$$-$(date +%s%N)"
}

# Parses "Submitted batch job <id>" out of a captured log. Only safe to
# call against a log that covers exactly one sbatch invocation - callers
# doing more than one per log should filter first.
function extract_job_id {
  grep -oE 'Submitted batch job [0-9]+' "$1" | tail -n1 | awk '{print $4}'
}

# Polls sacct until the job leaves an active state (PENDING/RUNNING/
# COMPLETING/REQUEUED) or `timeout` seconds pass. Echoes the terminal
# state (COMPLETED/FAILED/CANCELLED/...) or "TIMEOUT".
#
# An empty sacct result (accounting hasn't caught up with a job that
# just got submitted, or slurmdbd is momentarily behind) is treated as
# "still going," not "done" - the same caution sacct.py's query_sacct
# takes before trusting a blank result.
function wait_for_terminal_state {
  local job_id="$1" timeout="${2:-300}" waited=0 state
  while [ "$waited" -lt "$timeout" ]; do
    state=$(sacct -j "$job_id" --format=State --noheader --parsable2 2>/dev/null \
              | head -n1 | awk '{print $1}')
    case "$state" in
      ""|PENDING|RUNNING|COMPLETING|REQUEUED|RESIZING)
        ;;
      *)
        echo "$state"
        return 0
        ;;
    esac
    sleep 3
    waited=$((waited+3))
  done
  echo "TIMEOUT"
  return 1
}

# Polls squeue until the job reaches SLURM state $1 (e.g. "R" for
# running) or `timeout` seconds pass. Used for tests that need to
# interact with a job WHILE it's actually running (scancel, or a
# second job_queue_run invocation checking "is this already active").
function wait_for_squeue_state {
  local job_id="$1" want_state="$2" timeout="${3:-120}" waited=0 state
  while [ "$waited" -lt "$timeout" ]; do
    state=$(squeue -j "$job_id" -h -o "%t" 2>/dev/null)
    [ "$state" = "$want_state" ] && return 0
    [ -z "$state" ] && return 1   # job already left the queue entirely
    sleep 2
    waited=$((waited+2))
  done
  return 1
}

# Polls a job's metadata file until it actually contains a recorded
# slurm_job_id matching $2, or `timeout` seconds pass. Unlike
# wait_for_squeue_state (which only proves SLURM has started the batch
# script), this proves job_queue_run has ACTUALLY reached its
# record-submission call inside that script - closing the startup-lag
# race window (node prolog, sourcing hpclib.sh, conda activate, etc.)
# between "squeue says R" and "the script itself has done anything."
function wait_for_metadata_recorded {
  local metadata_path="$1" want_slurm_id="$2" timeout="${3:-60}" waited=0 got
  while [ "$waited" -lt "$timeout" ]; do
    if [ -f "$metadata_path" ]; then
      got=$(python3 -c "import json;print(json.load(open('$metadata_path')).get('slurm_job_id',''))" 2>/dev/null)
      [ "$got" = "$want_slurm_id" ] && return 0
    fi
    sleep 1
    waited=$((waited+1))
  done
  return 1
}

function wait_for_metadata_status {
  local metadata_path="$1" timeout="${2:-60}" waited=0 got
  while [ "$waited" -lt "$timeout" ]; do
    if [ -f "$metadata_path" ]; then
      got=$(python3 -c "import json;print(json.load(open('$metadata_path')).get('status',''))" 2>/dev/null)
      case "$got" in
        COMPLETED|FAILED|TIMEOUT|CANCELLED|NODE_FAIL)
          echo "$got"
          return 0
          ;;
      esac
    fi
    sleep 1
    waited=$((waited+1))
  done
  echo "TIMEOUT"
  return 1
}


# Writes a process_sbatch.sh whose submit()/restart() bodies are
# supplied by the caller as raw bash. Bakes in cluster-specific
# TEST_SBATCH_ARGS/TEST_SBATCH_TIME/TEST_SBATCH_MEM so every fixture
# schedules the same way.
function write_process_sbatch {
  local path="$1" submit_body="$2" restart_body="${3:-exit 1}"
  local hpclib_dir="$4"
  cat > "$path" <<EOF
#!/bin/bash
#SBATCH --time=${TEST_SBATCH_TIME} --mem=${TEST_SBATCH_MEM} --signal=B:TERM@30 ${TEST_SBATCH_ARGS}

CONFIG_FILE="\$1"

function submit {
${submit_body}
}

function restart {
${restart_body}
}

. "${hpclib_dir}/hpclib.sh"
job_queue_run
EOF
  chmod +x "$path"
}
