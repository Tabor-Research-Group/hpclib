#!/bin/bash

################################################################################
##
##  Configure hpclib settings
##    - root directory HPCLIB can be set in ~.bashrc
##    - HPCTUNNELS_DIR: directory to use for tunnels
##    - HPCSERVERS_DIR: directory to use for servers
##    - HPCSESSIONS_DIR: directory to use for session info
##

set -a # make all variables accessible to sbatch process

source ~/.bashrc
if [ "$HPCLIB_DIR" = "" ]; then
  HPCLIB_DIR=~/hpclib
fi
source $HPCLIB_DIR/hpclib.sh
if [ "$HPCTUNNELS_DIR" = "" ]; then
  HPCTUNNELS_DIR="$HPCLIB_DIR/tunnels"
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
##    - port: which port to expose
##    - conda environment: which python/git is used
##    - sbatch args: job config
##    - retries/wait time: how long to wait for the system to allocate the job
##    - initialization pause: delay to actually create the job (when swamped)
##

DEFAULT_PORT=8080
CONDA_ENVIRONMENT="default"
DEFAULT_SBATCH_ARGS="--time=0-8:00:00 --mem=1gb --ntasks=1"
JOB_CONNECT_RETRIES=60
JOB_CONNECT_RETRY_WAIT_TIME=1
JOB_INITIALIZATION_PAUSE=5

if [ -f "$HPCTUNNELS_DATA_DIR/config.sh" ]; then
  source "$HPCTUNNELS_DATA_DIR/config.sh"
fi

################################################################################
##
##  Configure tunel settings (edit in TUNNEL_NAME/tunnel_config.sh)
##    - TUNNEL_NAME: the name of the specific tunnel being built
##    - START_GIT_SERVER: whether or not to start a server to run git via ssh
##    - TUNNEL_DIR: where to find the tunnel config
##    - SESSIONS_DIR: where to log output from this session
##    - SBATCH_SCRIPT: the script to run via sbatch to start the process
##

TUNNEL_NAME="$1"
shift
PROCESS_PORT=8080
ENABLE_WEB_PROXY=true
START_GIT_SERVER=true
START_SLURM_SERVER=true
TUNNEL_DIR=$HPCTUNNELS_DIR/$TUNNEL_NAME
SESSIONS_DIR=$HPCSESSIONS_DIR/$TUNNEL_NAME
SBATCH_SCRIPT=$TUNNEL_DIR/sbatch_script.sh

if [ ! -f "$TUNNEL_DIR/sbatch_script.sh" ]; then
  echo "Tunnel $TUNNEL_DIR doesn't exist"
  exit 1
fi

if [ -f "$TUNNEL_DIR/tunnel_config.sh" ]; then
  source "$TUNNEL_DIR/tunnel_config.sh"
fi


################################################################################
##
##  Set up tunnel
##    - build a job id
##    - request sbatch job with those args,
##    - retries/wait time: how long to wait for the system to allocate the job
##    - initialization pause: delay to actually create the job (when swamped)
##

# build a randomize job name so we can correct to the right process
job_uuid=$(random_id)
job_name="$TUNNEL_NAME-$job_uuid"

# start sbatch script on correct port with given sbatch arg string
HOST_PORT=$(mcoptvalue "P:" "P" $@)
if [ "$HOST_PORT" = "" ];  then
    HOST_PORT=$DEFAULT_PORT
fi
if [ "$PROCESS_PORT" = "" ]; then
  PROCESS_PORT="$HOST_PORT"
fi

sbatch_args=$(mcargs "P:" $@)
if [ "$sbatch_args" = "" ]; then
    sbatch_args="$DEFAULT_SBATCH_ARGS"
fi

mkdir -p "$SESSIONS_DIR"
sbatch --job-name=$job_name --open-mode=append --out="$SESSIONS_DIR/session-%j.log" --export=ALL $sbatch_args "$SBATCH_SCRIPT"

function stop_git_server() {
  if [ "$GIT_SERVER_JOB" != "" ]; then
    kill $GIT_SERVER_JOB > /dev/null
    rm -f "$GIT_SOCKET_FILE"
  fi
}
function cancel_job() {
  scancel $SESSION_ID
}
function cleanup() {
  scancel $SESSION_ID
  stop_git_server
}

# wait for job to start and connect to node
SESSION_ID=$(get_job_id_by_name $job_name)
if [ "$SESSION_ID" = "" ]
    then echo "Job seems to have failed to start, check 'squeue -u <username>' to make sure this is the case"
    else

      SESSION_FILE="$SESSIONS_DIR/session-$SESSION_ID.log"

      if [ "$START_GIT_SERVER" = "true" ]; then
        export GIT_SOCKET_HOST=$(hostname)
        conda activate $CONDA_ENVIRONMENT
        python "$HPCSERVERS_DIR/git_server.py" &
        GIT_SERVER_JOB=$!
      fi
      
      trap "cleanup" 0 1 2 3

      if [ -f "$TUNNEL_DIR/preconnect.sh" ]; then
          source $TUNNEL_DIR/preconnect.sh
        fi

      POST_SCRIPT="$TUNNEL_DIR/postconnect.sh"
      if [ ! -f "$POST_SCRIPT" ]; then
          POST_SCRIPT="$HPCTUNNELS_DIR/postconnect.sh"
      fi
      TUNNEL_ENV_FIlE="$SESSIONS_DIR/env-$SESSION_ID.sh"
      declare -px > "$TUNNEL_ENV_FIlE"
      connect_to_job -P $HOST_PORT:$PROCESS_PORT -R $JOB_CONNECT_RETRIES -S $JOB_CONNECT_RETRY_WAIT_TIME -I $JOB_INITIALIZATION_PAUSE $SESSION_ID "source $TUNNEL_ENV_FIlE; source $POST_SCRIPT"
#      scancel $SESSION_ID
#      cleanup
fi
