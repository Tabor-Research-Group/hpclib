
function _ssh_connected {
  local ret;

  ssh -o "ControlPath=$1" -O check ":)" 2> /tmp/ssh_doop_doop
  ret=$(cat /tmp/ssh_doop_doop)
  rm /tmp/ssh_doop_doop
  if [[ "$ret" == "Master running"* ]]
    then
      echo "true"
    else
      echo "false"
  fi

}

SSH_FLAGS="46AaCfgKkMNnqPRTtVvXxYy:b:c:D:E:e:F:f:i:I:J:L:l:m:O:o:p:Q:S:W:w:";
# a dead connection (sleep, VPN change) errors out in ~2 minutes instead of hanging
HPCLIB_SSH_KEEPALIVE="${HPCLIB_SSH_KEEPALIVE--o ServerAliveInterval=30 -o ServerAliveCountMax=4}"
# how long a login (password, 2FA) is reused once nothing is using it; the console sets it per cluster
HPCLIB_SSH_PERSIST="${HPCLIB_SSH_PERSIST:-12h}"
SSH_LONG_FLAGS=""
function _ssh_like {
  local cmd="$1" opt flag value flags restart server servername
  local -a args keepalive
  args=()
  shift
  # Preserve argv, including spaces in IdentityFile/ProxyCommand and commands.
  # -R is hpclib's restart flag; all other options go to the native client.
  while [ "$#" -gt 0 ]; do
    opt="$1"; shift
    case "$opt" in
      --) break ;;
      -?*)
        flags="${opt#-}"
        while [ -n "$flags" ]; do
          flag="${flags:0:1}"; flags="${flags:1}"
          case "$flag" in
            R) restart=true ;;
            b|c|D|E|e|F|i|I|J|L|l|m|O|o|p|Q|S|W|w|B)
              if [ -n "$flags" ]; then value="$flags"; flags=""
              elif [ "$#" -gt 0 ]; then value="$1"; shift
              else echo "Missing value for -$flag" >&2; return 2; fi
              args+=("-$flag" "$value") ;;
            *) args+=("-$flag") ;;
          esac
        done ;;
      *) server="$opt"; break ;;
    esac
  done
  if [ -z "$server" ] && [ "$#" -gt 0 ]; then server="$1"; shift; fi
  if [ -z "$server" ]; then echo 'No SSH host provided' >&2; return 2; fi
  args+=(-o 'ControlMaster=auto' -o 'ControlPath=~/.ssh/connections/%r@%h:%p' -o "ControlPersist=$HPCLIB_SSH_PERSIST")
  read -r -a keepalive <<< "$HPCLIB_SSH_KEEPALIVE"
  args+=("${keepalive[@]}" "$server" "$@")
  # Retain the historical per-host restart marker behavior.
  server=${server%:*}
  servername=${server#*@}
  mkdir -p "$HOME/.ssh/connections"
  if [ "$restart" ]; then
    rm -f "$HOME/.ssh/connections/$servername"
  fi
  if [ "$HPCLIB_ECHO_COMMANDS" ]; then
    printf '%q ' "$cmd" "${args[@]}"; printf '\n'
  fi
  "$cmd" "${args[@]}"
}

SCP_FLAGS="UEZ:u:s:346BCpqrvF:i:l:o:P:S:R:";
SCP_LONG_FLAGS=""
function _scp_like {
  local cmd="$1";
  local args;
  local server;
  local base_opts;
  local conn_opt;
  local socket;
  local exists;
  local connected;
  local restart;
  shift

  base_opts=$(mcopts "$SCP_FLAGS" "$SCP_LONG_FLAGS" "o|R" $@);
  conn_opt=$(mcoptvalue "$SCP_FLAGS" "$SCP_LONG_FLAGS" "o" $@);
  restart=$(mcoptvalue "$SCP_FLAGS" "$SCP_LONG_FLAGS" "R" $@);
  args=($(mcargs "$SCP_FLAGS" "$SCP_LONG_FLAGS" $@));
  server=${args[1]}
  server=${server%:*}
  servername=${server#*@}

  SOCKET_DIR=~/.ssh/connections
  mkdir -p $SOCKET_DIR
  socket="$SOCKET_DIR/$servername"
  if [ "$restart" ]; then
    rm "$socket" 2> /dev/null
  fi
  # connected=$(_ssh_connected "$socket")
  conn_opt=$(_build_argstr "$conn_opt" "ControlPath=$socket")
  # conn_opt=$(_build_argstr "$conn_opt" "ControlMaster=auto")
  # conn_opt=$(_build_argstr "$conn_opt" "ControlPersist=4h")

  # if [[ "$connected" == "false" ]]; then
  #   ssh -M -f -N -o $conn_opt $server
  # fi

  mkdir -p ~/.ssh/connections
  if [ "$HPCLIB_ECHO_COMMANDS" ]; then
    echo $cmd $base_opts -o 'ControlMaster=auto' -o 'ControlPath=~/.ssh/connections/%r@%h:%p' -o "ControlPersist=$HPCLIB_SSH_PERSIST" $HPCLIB_SSH_KEEPALIVE ${args[@]}
  fi
  $cmd $base_opts -o 'ControlMaster=auto' -o 'ControlPath=~/.ssh/connections/%r@%h:%p' -o "ControlPersist=$HPCLIB_SSH_PERSIST" $HPCLIB_SSH_KEEPALIVE ${args[@]}

}

RSYNC_FLAGS="rlptgDaqbudLkKHEAXsF:f:o:R:";
RSYNC_LONG_FLAGS=""
function _rsync_like {
  local cmd="$1";
  local args;
  local server;
  local base_opts;
  local conn_opt;
  local socket;
  local exists;
  local connected;
  local restart;
  shift

  base_opts=$(mcopts "$RSYNC_FLAGS" "$RSYNC_LONG_FLAGS" "o|R" $@);
  conn_opt=$(mcoptvalue "$RSYNC_FLAGS" "$RSYNC_LONG_FLAGS" "o" $@);
  restart=$(mcoptvalue "$RSYNC_FLAGS" "$RSYNC_LONG_FLAGS" "R" $@);
  args=($(mcargs "$RSYNC_FLAGS" "$RSYNC_LONG_FLAGS" $@));
  server=${args[1]}
  server=${server%:*}
  servername=${server#*@}

  SOCKET_DIR=~/.ssh/connections
  mkdir -p $SOCKET_DIR
  socket="$SOCKET_DIR/$servername"
  if [ "$restart" ]; then
    rm "$socket" 2> /dev/null
  fi
  # connected=$(_ssh_connected "$socket")
  # conn_opt=$(_build_argstr "$conn_opt" "ControlPath=$socket")
  # conn_opt=$(_build_argstr "$conn_opt" "ControlMaster=auto")
  # conn_opt=$(_build_argstr "$conn_opt" "ControlPersist=4h")

  # if [[ "$connected" == "false" ]]; then
  #   ssh -M -f -N -o $conn_opt $server
  # fi
    mkdir -p ~/.ssh/connections
  if [ "$HPCLIB_ECHO_COMMANDS" ]; then
    echo $cmd $base_opts -e "ssh -o \'ControlMaster=auto\' -o \'ControlPath=~/.ssh/connections/%r@%h:%p\' -o \'ControlPersist=$HPCLIB_SSH_PERSIST\'" ${args[@]}
  fi
  $cmd $base_opts -e "ssh -o 'ControlMaster=auto' -o 'ControlPath=~/.ssh/connections/%r@%h:%p' -o 'ControlPersist=$HPCLIB_SSH_PERSIST'" ${args[@]}

}

function pssh {
  _ssh_like ssh "$@"
}

# Initialize login-to-compute authentication in the cluster's shared home.
# The local helper is shipped over the existing login: no REST tunnel or newer
# remote hpclib install is needed, and no private keys come back to this machine.
function setup_cluster_ssh {
  if [ "$#" -eq 0 ]; then
    echo 'usage: setup_cluster_ssh [ssh options] [user@]host' >&2
    return 2
  fi
  local helper="$HPCLIB_DIR/servers/ssh_keys.py"
  local remote='if [ -x "${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/rest/python" ]; then exec "${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/rest/python" - --setup; else exec python3 - --setup; fi'
  HPCLIB_ECHO_COMMANDS= pssh "$@" "$remote" < "$helper"
}
function pscp {
  _scp_like scp $@
}
function psftp {
  _ssh_like sftp "$@"
}
function psync {
  _rsync_like rsync $@
}

DEFAULT_FORWARDING_ADDRESS="127.0.0.1"
DEFAULT_FORWARDING_PORT="8666"
function fwd_spec {
  local addr;
  local target_addr;
  local port;
  local target_port;
  local spec="$1";
  local spec_array;
  local num_els;

  spec_array=($(echo "$spec" | tr ":" " "))
  num_els=${#spec_array[@]}

  case "$num_els" in
    0)
      ;;
    1)
      port=${spec_array[0]}
      ;;
    2)
      port=${spec_array[0]}
      target_port=${spec_array[1]}
      ;;
    3)
      addr=${spec_array[0]}
      port=${spec_array[1]}
      target_port=${spec_array[2]}
      ;;
    *)
      addr=${spec_array[0]}
      port=${spec_array[1]}
      target_addr=${spec_array[2]}
      target_port=${spec_array[3]}
      ;;
    esac

    if [ "$addr" = "" ]; then
      addr=$DEFAULT_FORWARDING_ADDRESS
    fi
    if [ "$port" = "" ]; then
      port=$DEFAULT_FORWARDING_PORT
    fi
    if [ "$target_addr" = "" ]; then
      target_addr="$addr"
    fi
    if [ "$target_port" = "" ]; then
      target_port="$port"
    fi

    echo "$addr:$port:$target_addr:$target_port"
}

function multi_fwd_spec {
  local type=$1;
  local forwarding=$2;
  local fwd;
  local ssh_args;

  forwarding_specs=($(echo "$forwarding" | tr "," " "))
  for fwd in ${forwarding_specs[@]}; do
    fwd=$(fwd_spec $fwd)
    ssh_args=$(_build_argstr "$ssh_args" "-$type $fwd")
  done

  echo "$ssh_args";
}

function pfwd {
  local port=$1
  shift
  _ssh_like ssh -fN -L $(fwd_spec $port) $@
}

# pssh/pscp/psftp/psync/fwd_spec/multi_fwd_spec/pfwd/listening_on: unchanged
