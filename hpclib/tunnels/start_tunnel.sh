#!/bin/bash --init-file

################################################################################
##
##  Configure hpclib settings
##    - root directory HPCLIB can be set in ~.bashrc
##    - HPCLIB_TUNNEL_PATH: colon-separated tunnel search path
##    - HPCLIB_TUNNEL_INSTALL_LOCATION: default install parent directory
##    - HPCSERVERS_DIR: directory to use for servers
##    - HPCSESSIONS_DIR: directory to use for session info
##

set -a # make all variables accessible to sbatch process

source ~/.bashrc
if [ "$HPCLIB_DIR" = "" ]; then
  # Resolve the real, absolute location of THIS script - not $0
  # (which can be wrong when sourced, or a relative path when
  # invoked as `bash path/to/start_tunnel.sh`) - following symlinks
  # manually since `readlink -f` isn't available on macOS's BSD
  # readlink. start_tunnel.sh always lives at hpclib_root/tunnels/,
  # so HPCLIB_DIR is one directory up from wherever this resolves to.
  _src="${BASH_SOURCE[0]:-$0}"
  while [ -h "$_src" ]; do
    _dir="$(cd -P "$(dirname "$_src")" >/dev/null 2>&1 && pwd)"
    _src="$(readlink "$_src")"
    case "$_src" in
      /*) ;;                      # already absolute
      *) _src="$_dir/$_src" ;;    # relative symlink target -> make absolute
    esac
  done
  _dir="$(cd -P "$(dirname "$_src")" >/dev/null 2>&1 && pwd)"
  HPCLIB_DIR="$(cd -P "$_dir/.." >/dev/null 2>&1 && pwd)"
  unset _src _dir
fi
source "$HPCLIB_DIR/hpclib.sh"
source "$HPCLIB_DIR/tunnels/instances.sh"
if [ "$HPCSERVERS_DIR" = "" ]; then
  HPCSERVERS_DIR="$HPCLIB_DIR/servers"
fi
if [ "$HPCTUNNELS_DATA_DIR" = "" ]; then
  HPCTUNNELS_DATA_DIR=~/.local/tunnels
fi
if [ "$HPCSESSIONS_DIR" = "" ]; then
  HPCSESSIONS_DIR="$HPCTUNNELS_DATA_DIR/sessions"
fi

################################################################################
##
##  Configure user specific defaults (edit in ~/.local/tunnels/config.sh)
##

DEFAULT_PORT=8080
CONDA_ENVIRONMENT="default"
CREATE_ENV_FILE=true
DEFAULT_SBATCH_ARGS="--time=0-8:00:00 --mem=1gb --ntasks=1"
JOB_CONNECT_RETRY_WAIT_TIME=2
JOB_INITIALIZATION_PAUSE=5

if [ -f "$HPCTUNNELS_DATA_DIR/config.sh" ]; then
  source "$HPCTUNNELS_DATA_DIR/config.sh"
fi

################################################################################
##
##  Parse command-line args FIRST, before tunnel_config.sh is sourced.
##  What's given here is remembered in CLI_* vars and re-applied AFTER
##  tunnel_config.sh runs, so command-line values always win regardless
##  of sourcing order - rather than depending on tunnel_config.sh not
##  clobbering them, which is what silently broke overrides before.
##

TUNNEL_NAME="$1"
shift

# Arguments after -- belong to the tunnel's sbatch script. Arguments
# before it configure the tunnel or are passed to sbatch itself.
START_TUNNEL_ARGS=()
TUNNEL_SCRIPT_ARGS=()
while [ "$#" -gt 0 ]; do
  if [ "$1" = "--" ]; then
    shift
    TUNNEL_SCRIPT_ARGS=("$@")
    break
  fi
  START_TUNNEL_ARGS+=("$1")
  shift
done
set -- "${START_TUNNEL_ARGS[@]}"

START_TUNNEL_FLAGS="fP:"
START_TUNNEL_LONG_FLAGS="port:,process-port:,env:,login-node"

CLI_HOST_PORT=$(mcoptvalue "$START_TUNNEL_FLAGS" "$START_TUNNEL_LONG_FLAGS" "P" "$@")
if [ -z "$CLI_HOST_PORT" ]; then
  CLI_HOST_PORT=$(mclongvalue "$START_TUNNEL_LONG_FLAGS" "port" "$@")
fi
CLI_PROCESS_PORT=$(mclongvalue "$START_TUNNEL_LONG_FLAGS" "process-port" "$@")
CLI_ENV=$(mclongvalue "$START_TUNNEL_LONG_FLAGS" "env" "$@")
CLI_LOGIN_NODE=$(mclongvalue "$START_TUNNEL_LONG_FLAGS" "login-node" "$@")
start_bg=$(mcoptvalue "$START_TUNNEL_FLAGS" "$START_TUNNEL_LONG_FLAGS" "f" "$@")

# Everything else - short or long, meant for sbatch (--mem=, --time=,
# --gres=, ...) rather than for us - passes through untouched and in
# original order.
CLI_SBATCH_ARGS=$(mcargs "$START_TUNNEL_FLAGS" "$START_TUNNEL_LONG_FLAGS" "$@")

################################################################################
##
##  Resolve through the same API available to hpclib.sh users.
##

if ! TUNNEL_DIR=$(resolve_tunnel "$TUNNEL_NAME"); then
  echo "Tunnel '$TUNNEL_NAME' not found in HPCLIB_TUNNEL_PATH ($HPCLIB_TUNNEL_PATH)" >&2
  exit 1
fi
SESSIONS_DIR=$HPCSESSIONS_DIR/$TUNNEL_NAME
SBATCH_SCRIPT="$TUNNEL_DIR/sbatch_script.sh"
PROCESS_PORT=8080
ENABLE_WEB_PROXY=true
START_GIT_SERVER=true
START_SLURM_SERVER=true

SHARED_INSTANCE=false        # true: attach to a running instance another job serves, if there is one
PROCESS_PORT_FROM_JOB=false  # true: the job picks its port and registers it (see instances.sh)
KEEP_INSTANCE=false          # true: closing the tunnel leaves the job running (for others to attach to)
RUN_ON_LOGIN_NODE=false      # true (or --login-node): run the sbatch script here, on the login node, not in a job

if tunnel_config_path=$(resolve_tunnel_file tunnel_config.sh); then
  source "$tunnel_config_path"
fi

# This tunnel's settings on this cluster (install paths and the like), written by setup_tunnel.sh --set or
# the console's tunnel settings: environment variables its install.sh and its job read.
TUNNEL_SETTINGS_FILE="$HPCTUNNELS_DATA_DIR/settings/$TUNNEL_NAME.sh"
if [ -f "$TUNNEL_SETTINGS_FILE" ]; then
  source "$TUNNEL_SETTINGS_FILE"
fi

################################################################################
##
##  Command-line values always win over whatever tunnel_config.sh set.
##

if [ -n "$CLI_PROCESS_PORT" ]; then PROCESS_PORT="$CLI_PROCESS_PORT"; fi
if [ -n "$CLI_HOST_PORT" ]; then DEFAULT_PORT="$CLI_HOST_PORT"; fi
if [ "$CLI_LOGIN_NODE" = "true" ]; then RUN_ON_LOGIN_NODE=true; fi

HOST_PORT="$DEFAULT_PORT"
if [ -z "$PROCESS_PORT" ]; then
  PROCESS_PORT="$HOST_PORT"
fi

# CLI sbatch args are APPENDED after the tunnel's defaults, not
# substituted for them - sbatch honors the LAST occurrence of a
# repeated flag, so `--mem=64gb` on the CLI correctly overrides a
# `--mem=30gb` baked into DEFAULT_SBATCH_ARGS, while every default flag
# you didn't mention (--time, --ntasks, ...) survives untouched.
sbatch_args="$DEFAULT_SBATCH_ARGS $CLI_SBATCH_ARGS"

# --env=NAME=value,NAME2=value2 rides into sbatch's own --export.
export_spec="ALL"
if [ -n "$CLI_ENV" ]; then
  export_spec="ALL,${CLI_ENV}"
fi

################################################################################
##
##  Set up tunnel
##

job_uuid=$(random_id)
job_name="$TUNNEL_NAME-$job_uuid"

mkdir -p "$SESSIONS_DIR"

# What this tunnel starts here carries the port in its environment, so a later tunnel (or stop_tunnel) can tell
# what is listening on it was left by one of yours (_hpclib_port_holders), however it was orphaned.
export HPCLIB_TUNNEL_PORT="$HOST_PORT"

# Anything an earlier tunnel on this port left running (its forward, its
# waiting page, its job) is stopped first; a port held by something else
# is an error rather than a tunnel that silently doesn't work. A tunnel whose
# service isn't one of these (a podman pod, say) clears what an earlier run of
# it left with its own clear_port.sh PORT, first.
if [ -f "$TUNNEL_DIR/clear_port.sh" ]; then
  bash "$TUNNEL_DIR/clear_port.sh" "$HOST_PORT" ||
    echo "hpclib: $TUNNEL_NAME's clear_port.sh failed; checking the port anyway" >&2
fi
if ! _hpclib_clear_port "$HOST_PORT"; then
  exit 1
fi
_hpclib_record_port "$HOST_PORT" "$$"

STATUS_FILE="$SESSIONS_DIR/status-$job_uuid.txt"
echo "submitting job..." > "$STATUS_FILE"

################################################################################
##
##  On the login node (--login-node, or RUN_ON_LOGIN_NODE=true): for sites that
##  would rather a small service ran on the login node than in a job holding a
##  share of a compute node. The tunnel's sbatch script runs here as a child of
##  this script, listening on the forwarded port itself; it ends when the tunnel
##  does (stop_tunnel, Ctrl+C, a dropped ssh), as a job would be cancelled. There
##  is no job, so sbatch options (--time, --mem, ...) don't apply.
##

if [ "$RUN_ON_LOGIN_NODE" = "true" ]; then
  if [ "$SHARED_INSTANCE" = "true" ] || [ "$PROCESS_PORT_FROM_JOB" = "true" ]; then
    echo "start_tunnel: $TUNNEL_NAME shares its service between jobs or registers its port from its job;" \
      "it can't run on the login node" >&2
    exit 1
  fi
  if [ "$start_bg" = "true" ]; then
    echo "start_tunnel: -f (background) isn't supported with --login-node; the service ends with the tunnel" >&2
    exit 1
  fi
  if [ -n "${CLI_SBATCH_ARGS// /}" ]; then
    echo "hpclib: on the login node there is no job; ignoring the sbatch options: ${CLI_SBATCH_ARGS# }" >&2
  fi
  PROCESS_PORT="$HOST_PORT"
  TUNNEL_ON_LOGIN_NODE=true
  SESSION_ID="login-$job_uuid"
  SESSION_FILE="$SESSIONS_DIR/session-$SESSION_ID.log"
  # --env=NAME=value,NAME2=value2, which sbatch's --export would have set
  if [ -n "$CLI_ENV" ]; then
    IFS=',' read -r -a login_env <<< "$CLI_ENV"
    for pair in "${login_env[@]}"; do
      case "$pair" in
        [A-Za-z_]*=*) export "$pair" ;;
        *) echo "start_tunnel: ignoring --env entry '$pair' (expected NAME=value)" >&2 ;;
      esac
    done
  fi
  echo "running on the login node $(hostname -s), not in a job" > "$STATUS_FILE"
  echo "hpclib: running $TUNNEL_NAME on the login node $(hostname -s) (no SLURM job), port $PROCESS_PORT; log: $SESSION_FILE"
  bash "$SBATCH_SCRIPT" "${TUNNEL_SCRIPT_ARGS[@]}" >> "$SESSION_FILE" 2>&1 < /dev/null &
  LOGIN_PID=$!
  login_cleanup() {
    kill -TERM "$LOGIN_PID" 2>/dev/null
    pkill -TERM -P $$ 2>/dev/null   # the log's tail
    _hpclib_forget_port "$HOST_PORT" "$$"
  }
  trap login_cleanup 0
  trap 'exit 130' 1 2 3 15
  tail -f -n +1 --pid="$LOGIN_PID" "$SESSION_FILE" 2>/dev/null &
  wait "$LOGIN_PID"
  login_status=$?
  sleep 1   # the tail's last lines
  echo "hpclib: $TUNNEL_NAME on the login node has ended (exit $login_status)"
  exit "$login_status"
fi

# A shared service (a database, say) that another job already runs: connect to it rather than starting
# another. The tunnel then never cancels that job.
TUNNEL_ATTACHED=false
if [ "$SHARED_INSTANCE" = "true" ] && instance=$(tunnel_find_instance "$TUNNEL_NAME"); then
  read -r SESSION_ID job_node PROCESS_PORT <<< "$instance"
  TUNNEL_ATTACHED=true
  echo "hpclib: attaching to the running $TUNNEL_NAME instance: job $SESSION_ID on $job_node, port $PROCESS_PORT"
fi
cancels_job() { [ "$TUNNEL_ATTACHED" != "true" ] && [ "$KEEP_INSTANCE" != "true" ]; }

# Bind the forwarded port to a "please wait" page RIGHT NOW, before
# sbatch is even called. The local `ssh -L` connects to this login
# node the moment the SSH session opens and needs SOMETHING listening
# on this port immediately, or the browser just sees
# connection-refused while SLURM queues the real job. Killed below the
# instant the real compute node is reachable.
if [ "$TUNNEL_ATTACHED" != "true" ]; then
  python3 "$HPCSERVERS_DIR/waiting_shim.py" "$HOST_PORT" "$STATUS_FILE" "$TUNNEL_NAME" > "$STATUS_FILE" &
  SHIM_PID=$!

  # --parsable: the job id comes straight from sbatch, whatever else the site's
  # job_submit plugin or squeue defaults print
  submit_out=$(sbatch --parsable --job-name=$job_name --open-mode=append --out="$SESSIONS_DIR/session-%j.log" \
    --export="$export_spec" $sbatch_args "$SBATCH_SCRIPT" "${TUNNEL_SCRIPT_ARGS[@]}")
  SUBMITTED_ID=$(printf '%s\n' "$submit_out" | grep -Eo '^[0-9]+' | tail -n 1)
  [ -n "$SUBMITTED_ID" ] && echo "Submitted batch job $SUBMITTED_ID"
fi

function stop_git_server() {
  if [ "$GIT_SERVER_JOB" != "" ]; then
    kill $GIT_SERVER_JOB > /dev/null
  fi
}
function stop_shim() {
  if [ "$SHIM_PID" != "" ]; then
    kill "$SHIM_PID" 2>/dev/null
    wait "$SHIM_PID" 2>/dev/null
  fi
}
function cleanup() {
  if cancels_job; then scancel $SESSION_ID 2>/dev/null; fi
  stop_git_server
  stop_shim
  pkill -TERM -P $$ 2>/dev/null   # e.g. the forward to the compute node
  _hpclib_forget_port "$HOST_PORT" "$$"
}
trap cleanup 0 1 2 3   # Ctrl+C locally now also cleans up the shim

if [ "$TUNNEL_ATTACHED" != "true" ]; then
  SESSION_ID="$SUBMITTED_ID"
  [ -n "$SESSION_ID" ] || SESSION_ID=$(get_job_id_by_name $job_name)
fi
if [ "$SESSION_ID" = "" ]
    then
      echo "job seems to have failed to submit; check 'squeue -u <username>'" > "$STATUS_FILE"
      echo "Job seems to have failed to start, check 'squeue -u <username>' to make sure this is the case"
      stop_shim
    else

      SESSION_FILE="$SESSIONS_DIR/session-$SESSION_ID.log"
      # stop_tunnel cancels the recorded job: only one this tunnel owns
      if cancels_job; then _hpclib_record_port "$HOST_PORT" "$$" "$SESSION_ID"; fi
      echo "job $SESSION_ID submitted, waiting for a node..." > "$STATUS_FILE"

      if [ "$START_GIT_SERVER" = "true" ]; then
        export GIT_SOCKET_PORT=$(random_port 10000 65535)
        export GIT_SOCKET_HOST=$(hostname)
        if [ -n "$CONDA_ENVIRONMENT" ] && type conda > /dev/null 2>&1; then
          conda activate "$CONDA_ENVIRONMENT" || true
        fi
        python "$HPCSERVERS_DIR/git_server.py" &
        GIT_SERVER_JOB=$!
      fi

      # No fixed retry cap on purpose: the shim covers the browser
      # the whole time, so there's no reason to give up after N tries
      # the way the old bounded retry loop did. Ctrl+C is the way out.
      job_node=""
      poll=0
      while [ -z "$job_node" ]; do
        job_node=$(get_job_node "$SESSION_ID" 2>/dev/null)
        if [ -z "$job_node" ]; then
          # A job that ended before it got going (a failing setup step, say) leaves the
          # queue: report how it ended and the end of its log, rather than waiting forever.
          if [ -z "$(squeue -j "$SESSION_ID" -h -o "%T" 2>/dev/null)" ]; then
            final=$(sacct -j "$SESSION_ID" -X -n -P -o State,ExitCode 2>/dev/null | head -n 1 | tr '|' ' ')
            echo "job $SESSION_ID ended before the tunnel connected (${final:-no longer in the queue})" > "$STATUS_FILE"
            echo "Job $SESSION_ID ended before the tunnel connected (${final:-no longer in the queue})." >&2
            if [ -s "$SESSION_FILE" ]; then
              echo "The end of its log, $SESSION_FILE:" >&2
              tail -n 20 "$SESSION_FILE" | sed 's/^/  /' >&2
            fi
            exit 1
          fi
          reason=$(squeue -j "$SESSION_ID" -h -o "%R" 2>/dev/null)
          echo "job $SESSION_ID queued (${reason:-waiting}) - poll #$poll" > "$STATUS_FILE"
          poll=$((poll+1))
          sleep "$JOB_CONNECT_RETRY_WAIT_TIME"
        fi
      done
      # A job that picks its own port (PROCESS_PORT_FROM_JOB) registers it once its service listens.
      if [ "$TUNNEL_ATTACHED" != "true" ] && [ "$PROCESS_PORT_FROM_JOB" = "true" ]; then
        waited=0
        until reported=$(tunnel_instance_port "$TUNNEL_NAME" "$SESSION_ID"); do
          if [ -z "$(squeue -j "$SESSION_ID" -h -o "%T" 2>/dev/null)" ]; then
            echo "job $SESSION_ID ended before it reported its port" > "$STATUS_FILE"
            echo "Job $SESSION_ID ended before it reported its port; see $SESSION_FILE" >&2
            exit 1
          fi
          if [ "$waited" -ge "${PROCESS_PORT_WAIT:-900}" ]; then
            echo "Job $SESSION_ID did not report its port within ${PROCESS_PORT_WAIT:-900} s; see $SESSION_FILE" >&2
            exit 1
          fi
          echo "job $SESSION_ID is on $job_node, starting its service..." > "$STATUS_FILE"
          sleep "$JOB_CONNECT_RETRY_WAIT_TIME"
          waited=$((waited + JOB_CONNECT_RETRY_WAIT_TIME))
        done
        PROCESS_PORT="$reported"
        echo "hpclib: job $SESSION_ID serves $TUNNEL_NAME on $job_node, port $PROCESS_PORT"
      fi
      echo "node $job_node is up, connecting..." > "$STATUS_FILE"

      # Free the port so the real forward below can bind it.
      stop_shim

      if [ -f "$TUNNEL_DIR/preconnect.sh" ]; then
          source $TUNNEL_DIR/preconnect.sh
        fi

      POST_SCRIPT=$(resolve_tunnel_file postconnect.sh)
      if [ "$CREATE_ENV_FILE" = "true" ]; then
        TUNNEL_ENV_FIlE="$SESSIONS_DIR/env-$SESSION_ID.sh"
        CURRENT_TUNNEL_ENV_FIlE="$SESSIONS_DIR/activate.sh"
        declare -px > "$TUNNEL_ENV_FIlE"
        cp "$TUNNEL_ENV_FIlE" "$CURRENT_TUNNEL_ENV_FIlE"
      fi

      if [ "$start_bg" = "true" ];
          then
            echo "SLURM JOB: $SESSION_ID; GIT SERVER PID: $GIT_SERVER_JOB"
            ssh_flags="-f"
          else
            ssh_flags="-t"
      fi

      # job_node is already confirmed, so connect_to_job's own internal
      # wait_for_job_node succeeds almost immediately - the small -R/-S
      # here is just a formality, not the real wait.
      connect_to_job $ssh_flags -P $HOST_PORT:$PROCESS_PORT -R 10 -S "$JOB_CONNECT_RETRY_WAIT_TIME" -I $JOB_INITIALIZATION_PAUSE $SESSION_ID "source $TUNNEL_ENV_FIlE; source $POST_SCRIPT"

      if [ "$start_bg" = "true" ];
        then
          echo "Connected to job $SESSION_ID"
        else
          cleanup
      fi
fi
