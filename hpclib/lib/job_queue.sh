# lib/job_queue.sh
#
# Bridges a single, self-contained sbatch script to the `job_queue`
# Python package (job_queue/ - see job_queue/README.md) so tracking
# submit/restart/failure/completion needs only ONE call -
# `job_queue_run` - at the bottom of that script, instead of the
# separate sbatch_wrapper.sh/submit.sh/restart.sh/job_runner.sh/
# checkpoint.sh files `job_queue init` scaffolds.
#
# Expected shape of the calling script:
#
#   #SBATCH --time=... --signal=B:TERM@60   (see NOTE below)
#
#   submit()     { ... }   # required: fresh-run work command
#   restart()    { ... }   # required IF a restart ever actually
#                           # happens; a job that always succeeds on
#                           # its first try never needs one
#   checkpoint() { ... }   # optional: run on SIGTERM/exit, before
#                           # submit()/restart()'s process is torn down
#   failed()     { ... }   # optional: run after a failed exit
#   completed()  { ... }   # optional: run after a clean exit
#
#   . ~/hpclib/hpclib.sh
#   job_queue_run
#
# Any of the five can instead be a same-named sibling file
# (submit.sh, restart.sh, checkpoint.sh, failed.sh, completed.sh) next
# to the sbatch script rather than an inline function - see
# _jq_dispatch/_jq_call_optional below for exactly how those are
# loaded and invoked, and how both styles end up equally
# interruptible.
#
# NOTE on --signal: SLURM directives are parsed before the job starts,
# so job_queue_run can't add `--signal=B:TERM@60` for you retroactively
# - include it yourself among the #SBATCH options if you want
# checkpoint() to run before a time-limit kill. Same requirement
# job_runner.sh had; it's just on the script itself now, not a nested
# job.

JOB_QUEUE_CHECKPOINT_TIMEOUT="${JOB_QUEUE_CHECKPOINT_TIMEOUT:-45}"

function _jq_have_fn {
  declare -F "$1" >/dev/null 2>&1
}

# Directory of the ORIGINAL invoking script (the sbatch script, not
# this file) - the last entry in BASH_SOURCE is always the outermost
# file in the current source chain. Same symlink-following logic as
# HPCLIB_DIR's own self-location inference, for the same reason: a
# relative path or symlink shouldn't break lookup of sibling
# submit.sh/restart.sh/etc files.
function _jq_script_dir {
  local src="${BASH_SOURCE[${#BASH_SOURCE[@]}-1]}"
  local dir
  while [ -h "$src" ]; do
    dir="$(cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd)"
    src="$(readlink "$src")"
    case "$src" in
      /*) ;;
      *) src="$dir/$src" ;;
    esac
  done
  cd -P "$(dirname "$src")" >/dev/null 2>&1 && pwd
}

# Starts hook $1 (submit/restart) in the BACKGROUND, directly in the
# CURRENT shell - not inside a `$(...)` command substitution, which
# would make it a grandchild of a throwaway subshell and un-`wait`-able
# from here. Sets the global JQ_WORK_PID rather than echoing it, for
# exactly that reason.
#
# Prefers an already-defined function named $1. Falls back to sourcing
# "$JQ_SCRIPT_DIR/$1.sh" if present, backgrounding that whole
# `( source ... )` subshell as one unit - which works identically
# whether that file defines a same-named function (called immediately
# after sourcing) or just runs its work inline at the top level (the
# old submit.sh/restart.sh template style). Returns non-zero only if
# neither a function nor a file was found at all.
function _jq_dispatch {
  local name="$1"
  if _jq_have_fn "$name"; then
    "$name" &
    JQ_WORK_PID=$!
    return 0
  fi
  local file="$JQ_SCRIPT_DIR/$name.sh"
  if [ -f "$file" ]; then
    (
      source "$file"
      if declare -F "$name" >/dev/null 2>&1; then
        "$name"
      fi
    ) &
    JQ_WORK_PID=$!
    return 0
  fi
  return 1
}

# Optional hooks (checkpoint/failed/completed) run in the FOREGROUND,
# synchronously - unlike submit/restart there's nothing concurrent for
# a slow one to block, so no backgrounding/wait complexity is needed
# here. Absence is not an error: these are genuinely optional.
function _jq_call_optional {
  local name="$1"; shift
  if _jq_have_fn "$name"; then
    "$name" "$@"
    return $?
  fi
  local file="$JQ_SCRIPT_DIR/$name.sh"
  if [ -f "$file" ]; then
    ( source "$file"; if declare -F "$name" >/dev/null 2>&1; then "$name" "$@"; fi )
    return $?
  fi
  return 0
}

# Polls a backgrounded checkpoint() hook rather than shelling out to
# GNU `timeout` (not present on macOS by default, and this file is
# sourced there too via hpclib.sh even though it only ever RUNS on a
# cluster) - keeps the whole thing dependency-free.
function _jq_run_checkpoint_with_timeout {
  _jq_call_optional checkpoint &
  local ckpt_pid=$! waited=0
  while kill -0 "$ckpt_pid" 2>/dev/null; do
    sleep 1
    waited=$((waited+1))
    if [ "$waited" -ge "$JOB_QUEUE_CHECKPOINT_TIMEOUT" ]; then
      kill -TERM "$ckpt_pid" 2>/dev/null
      echo "[job_queue_run] checkpoint hook exceeded ${JOB_QUEUE_CHECKPOINT_TIMEOUT}s, killing it" >&2
      break
    fi
  done
  wait "$ckpt_pid" 2>/dev/null
}

function job_queue {
  python -m job_queue "$@"
}

function job_queue_run {
  JOB_NAME="${JOB_NAME:-${SLURM_JOB_NAME:-$(basename "$(_jq_script_dir)")}}"
  METADATA="${METADATA:-metadata/${JOB_NAME}.json}"
  JQ_SCRIPT_DIR="$(_jq_script_dir)"

  local status_log status
  status_log=$(mktemp)
  status=$(job_queue status --metadata "$METADATA" --job-name "$JOB_NAME" 2>"$status_log")

  case "$status" in
    running)
      echo "Job ${JOB_NAME} is already active; nothing to do."
      cat "$status_log"; rm -f "$status_log"
      return 0
      ;;
    complete)
      echo "Job ${JOB_NAME} has already finished."
      rm -f "$status_log"
      return 0
      ;;
    error)
      echo "Job ${JOB_NAME} failed permanently:" >&2
      cat "$status_log" >&2
      rm -f "$status_log"
      exit 1
      ;;
    submit|restart)
      ;;
    *)
      echo "Unrecognized status '${status}' from job_queue status" >&2
      cat "$status_log" >&2
      rm -f "$status_log"
      exit 1
      ;;
  esac
  rm -f "$status_log"

  # Fail fast, BEFORE recording a submission, if the needed hook
  # genuinely doesn't exist - matches the spec's "if restart isn't
  # supplied and the job must be restarted, just terminate" behavior,
  # and applies the same courtesy to a missing submit().
  if ! _jq_have_fn "$status" && [ ! -f "$JQ_SCRIPT_DIR/$status.sh" ]; then
    echo "No ${status}() function or ${status}.sh found for ${JOB_NAME}; cannot proceed." >&2
    job_queue record-result --metadata "$METADATA" --status failed --error-message "no ${status}() defined"
    exit 1
  fi

  local slurm_id="${SLURM_JOB_ID:-}"
  if [ -z "$slurm_id" ]; then
    echo "Warning: SLURM_JOB_ID is unset (not running under sbatch?) - recording a placeholder id" >&2
    slurm_id=0
  fi
  job_queue record-submission --metadata "$METADATA" --slurm-job-id "$slurm_id" --script "${status}.sh"

  JQ_FINALIZED=0
  JQ_WORK_PID=""

  function _jq_finalize {
    local exit_code="$1"
    if [ "$JQ_FINALIZED" -eq 1 ]; then return; fi
    JQ_FINALIZED=1
    if [ "$exit_code" -eq 0 ]; then
      _jq_call_optional completed
      job_queue record-result --metadata "$METADATA" --status completed --exit-code 0
      echo "Job ${JOB_NAME} completed."
    else
      local msg="exited with status ${exit_code}"
      if [ "$exit_code" -eq 143 ]; then
        msg="terminated by SIGTERM (time limit or preemption)"
      fi
      _jq_call_optional failed "$exit_code"
      job_queue record-result --metadata "$METADATA" --status failed --exit-code "$exit_code" --error-message "$msg"
      echo "Job ${JOB_NAME} failed (exit ${exit_code})." >&2
    fi
  }

  function _jq_on_term {
    echo "[job_queue_run] received SIGTERM - stopping work and checkpointing" >&2
    if [ -n "$JQ_WORK_PID" ]; then
      kill -TERM "$JQ_WORK_PID" 2>/dev/null || true
      wait "$JQ_WORK_PID" 2>/dev/null
    fi
    _jq_run_checkpoint_with_timeout
    _jq_finalize 143
    exit 143
  }
  trap _jq_on_term TERM
  trap '_jq_finalize $?' EXIT

  _jq_dispatch "$status"
  wait "$JQ_WORK_PID"
  local work_status=$?

  trap - TERM EXIT
  _jq_finalize "$work_status"
  exit "$work_status"
}