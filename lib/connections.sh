
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
function _ssh_like {
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

  base_opts=$(mcopts "$SSH_FLAGS" "o|R" $@);
  conn_opt=$(mcoptvalue "$SSH_FLAGS" "o" $@);
  restart=$(mcoptvalue "$SSH_FLAGS" "R" $@);
  args=($(mcargs "$SSH_FLAGS" $@));
  server=${args[0]}
  server=${server%:*}
  servername=${server#*@}

  SOCKET_DIR=~/.ssh/connections
  mkdir -p $SOCKET_DIR
  socket="$SOCKET_DIR/$servername"
  if [ "$restart" ]; then
    rm "$socket" 2> /dev/null
  fi
  # connected=$(_ssh_connected "$socket")
  # conn_opt=$(_build_argstr "$conn_opt" "ControlMaster=auto")
  # conn_opt=$(_build_argstr "$conn_opt" "ControlPersist=4h")
  # conn_opt=$(_build_argstr "$conn_opt" "ControlPath=$socket")

  # if [[ "$connected" == "false" ]]; then
  #   ssh -M -f -N $base_opts -o $conn_opt $server
  # fi

  # echo "$cmd $base_opts -o $conn_opt ${args[@]}"
  mkdir -p ~/.ssh/connections
  if [ "$HPCLIB_ECHO_COMMANDS" ]; then
    echo $cmd $base_opts -o 'ControlMaster=auto' -o 'ControlPath=~/.ssh/connections/%r@%h:%p' -o 'ControlPersist=4h' ${args[@]}
  fi
  $cmd $base_opts -o 'ControlMaster=auto' -o 'ControlPath=~/.ssh/connections/%r@%h:%p' -o 'ControlPersist=4h' ${args[@]}

}

SCP_FLAGS="UEZ:u:s:346BCpqrvF:i:l:o:P:S:R:";
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

  base_opts=$(mcopts "$SCP_FLAGS" "o|R" $@);
  conn_opt=$(mcoptvalue "$SCP_FLAGS" "o" $@);
  restart=$(mcoptvalue "$SCP_FLAGS" "R" $@);
  args=($(mcargs "$SCP_FLAGS" $@));
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
    echo $cmd $base_opts -o 'ControlMaster=auto' -o 'ControlPath=~/.ssh/connections/%r@%h:%p' -o 'ControlPersist=4h' ${args[@]}
  fi
  $cmd $base_opts -o 'ControlMaster=auto' -o 'ControlPath=~/.ssh/connections/%r@%h:%p' -o 'ControlPersist=4h' ${args[@]}

}

RSYNC_FLAGS="rlptgDaqbudLkKHEAXsF:f:o:R:";
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

  base_opts=$(mcopts "$RSYNC_FLAGS" "o|R" $@);
  conn_opt=$(mcoptvalue "$RSYNC_FLAGS" "o" $@);
  restart=$(mcoptvalue "$RSYNC_FLAGS" "R" $@);
  args=($(mcargs "$RSYNC_FLAGS" $@));
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
    echo $cmd $base_opts -e "ssh -o \'ControlMaster=auto\' -o \'ControlPath=~/.ssh/connections/%r@%h:%p\' -o \'ControlPersist=4h\'" ${args[@]}
  fi
  $cmd $base_opts -e "ssh -o 'ControlMaster=auto' -o 'ControlPath=~/.ssh/connections/%r@%h:%p' -o 'ControlPersist=4h'" ${args[@]}

}

function pssh {
  _ssh_like ssh $@
}
function pscp {
  _scp_like scp $@
}
function psftp {
  _ssh_like sftp $@
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

function listening_on {
  local port=$1
  echo $(lsof -iTCP -sTCP:LISTEN -n -P | grep :$port)
}