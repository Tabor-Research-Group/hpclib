#!/bin/bash --init-file

################################################################################
##
##  Configure hpclib settings
##    - root directory HPCLIB can be set in ~.bashrc
##    - HPCTUNNELS_DIR: tunnels shipped with hpclib (overwritten by psync)
##    - USER_TUNNEL_DIR: your own persistent tunnels, checked FIRST
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
source $HPCLIB_DIR/hpclib.sh
if [ "$HPCTUNNELS_DIR" = "" ]; then
  HPCTUNNELS_DIR="$HPCLIB_DIR/tunnels"
fi
if [ "$USER_TUNNEL_DIR" = "" ]; then
  USER_TUNNEL_DIR="$HOME/.local/share/hpclib/tunnels"
fi
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

START_TUNNEL_FLAGS="fP:"
START_TUNNEL_LONG_FLAGS="port:,process-port:,env:"

CLI_HOST_PORT=$(mcoptvalue "$START_TUNNEL_FLAGS" "$START_TUNNEL_LONG_FLAGS" "P" "$@")
if [ -z "$CLI_HOST_PORT" ]; then
  CLI_HOST_PORT=$(mclongvalue "$START_TUNNEL_LONG_FLAGS" "port" "$@")
fi
CLI_PROCESS_PORT=$(mclongvalue "$START_TUNNEL_LONG_FLAGS" "process-port" "$@")
CLI_ENV=$(mclongvalue "$START_TUNNEL_LONG_FLAGS" "env" "$@")
start_bg=$(mcoptvalue "$START_TUNNEL_FLAGS" "$START_TUNNEL_LONG_FLAGS" "f" "$@")

# Everything else - short or long, meant for sbatch (--mem=, --time=,
# --gres=, ...) rather than for us - passes through untouched and in
# original order.
CLI_SBATCH_ARGS=$(mcargs "$START_TUNNEL_FLAGS" "$START_TUNNEL_LONG_FLAGS" "$@")

################################################################################
##
##  Resolve which tunnel to use: USER_TUNNEL_DIR (yours, persistent)
##  takes precedence over HPCTUNNELS_DIR (hpclib's, overwritten by psync).
##

if [ -f "$USER_TUNNEL_DIR/$TUNNEL_NAME/sbatch_script.sh" ]; then
  TUNNEL_DIR="$USER_TUNNEL_DIR/$TUNNEL_NAME"
else
  TUNNEL_DIR="$HPCTUNNELS_DIR/$TUNNEL_NAME"
fi
SESSIONS_DIR=$HPCSESSIONS_DIR/$TUNNEL_NAME
SBATCH_SCRIPT="$TUNNEL_DIR/sbatch_script.sh"
PROCESS_PORT=8080
ENABLE_WEB_PROXY=true
START_GIT_SERVER=true
START_SLURM_SERVER=true

if [ ! -f "$SBATCH_SCRIPT" ]; then
  echo "Tunnel '$TUNNEL_NAME' not found in $USER_TUNNEL_DIR or $HPCTUNNELS_DIR"
  exit 1
fi

# Generic lookup for everything below - and exported, so
# sbatch_script.sh templates (which run as their own sbatch-launched
# process, not sourced from here) can call it too. User copy, then this
# tunnel's shipped copy, then hpclib's shared default. First match wins.
function resolve_tunnel_file {
  local name="$1"
  local candidate
  for candidate in \
    "$USER_TUNNEL_DIR/$TUNNEL_NAME/$name" \
    "$TUNNEL_DIR/$name" \
    "$HPCTUNNELS_DIR/$name"
  do
    if [ -f "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}
export -f resolve_tunnel_file

if tunnel_config_path=$(resolve_tunnel_file tunnel_config.sh); then
  source "$tunnel_config_path"
fi

################################################################################
##
##  Command-line values always win over whatever tunnel_config.sh set.
##

if [ -n "$CLI_PROCESS_PORT" ]; then PROCESS_PORT="$CLI_PROCESS_PORT"; fi
if [ -n "$CLI_HOST_PORT" ]; then DEFAULT_PORT="$CLI_HOST_PORT"; fi

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
STATUS_FILE="$SESSIONS_DIR/status-$job_uuid.txt"
echo "submitting job..." > "$STATUS_FILE"

# Bind the forwarded port to a "please wait" page RIGHT NOW, before
# sbatch is even called. The local `ssh -L` connects to this login
# node the moment the SSH session opens and needs SOMETHING listening
# on this port immediately, or the browser just sees
# connection-refused while SLURM queues the real job. Killed below the
# instant the real compute node is reachable.
python3 "$HPCSERVERS_DIR/waiting_shim.py" "$HOST_PORT" "$STATUS_FILE" "$TUNNEL_NAME" > "$STATUS_FILE" &
SHIM_PID=$!

sbatch --job-name=$job_name --open-mode=append --out="$SESSIONS_DIR/session-%j.log" --export="$export_spec" $sbatch_args "$SBATCH_SCRIPT"

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
  scancel $SESSION_ID 2>/dev/null
  stop_git_server
  stop_shim
}
trap cleanup 0 1 2 3   # Ctrl+C locally now also cleans up the shim

SESSION_ID=$(get_job_id_by_name $job_name)
if [ "$SESSION_ID" = "" ]
    then
      echo "job seems to have failed to submit; check 'squeue -u <username>'" > "$STATUS_FILE"
      echo "Job seems to have failed to start, check 'squeue -u <username>' to make sure this is the case"
      stop_shim
    else

      SESSION_FILE="$SESSIONS_DIR/session-$SESSION_ID.log"
      echo "job $SESSION_ID submitted, waiting for a node..." > "$STATUS_FILE"

      if [ "$START_GIT_SERVER" = "true" ]; then
        export GIT_SOCKET_PORT=$(random_port 10000 65535)
        export GIT_SOCKET_HOST=$(hostname)
        conda activate $CONDA_ENVIRONMENT
        python "$HPCSERVERS_DIR/git_server.py" &
        GIT_SERVER_JOB=$!
      fi

      # No fixed retry cap on purpose: the shim covers the browser
      # the whole time, so there's no reason to give up after N tries
      # the way the old bounded retry loop did. Ctrl+C is the way out.
      job_node=""
      poll=0
      while [ -z "$job_node" ]; do
        job_node=$(get_job_node "$SESSION_ID")
        if [ -z "$job_node" ]; then
          reason=$(squeue -j "$SESSION_ID" -h -o "%R" 2>/dev/null)
          echo "job $SESSION_ID queued (${reason:-waiting}) - poll #$poll" > "$STATUS_FILE"
          poll=$((poll+1))
          sleep "$JOB_CONNECT_RETRY_WAIT_TIME"
        fi
      done
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