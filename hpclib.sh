MAIN_USER=$(whoami)
MAIN_PARTITION=""

function _get_user {
  local user="$1";

  if [ "$user" = "" ]
    then user=$MAIN_USER;
  fi;
  echo "$user"
}

function _build_argstr {
  local argstr=$1;
  local extra=$2;
  local flag=$3;

  if [ "$extra" != "" ]; then
    if [ "$flag" != "" ]; then
        if [ "$flag" = "--*" ]
          then extra="$flag=$extra"
          else  extra="$flag $extra"
        fi
    fi
  fi
  if [ "$extra" != "" ]; then
    if [ "$argstr" = "" ]
      then 
        argstr="$extra"
      else 
        argstr="$argstr $extra"
    fi
  fi

  echo "$argstr"
}

MCDEFAULT_SQUEUE_FORMAT="%.9i %.32j %.8u %.2t %.10M %.5C %.6D %R"
UQUEUE_FLAGS="u:p:A:f:"
function uqueue {
  local user;
  local partition;
  local account;
  local format;
  local argstr

  user=$(mcoptvalue "$UQUEUE_FLAGS" 'u' $@);
  paritition=$(mcoptvalue "$UQUEUE_FLAGS" 'p' $@);
  account=$(mcoptvalue "$UQUEUE_FLAGS" 'A' $@);
  format=$(mcoptvalue "$UQUEUE_FLAGS" 'f' $@);
  if [ "$user" = "" ]
    then user=$MCDEFAULT_USER
  fi
  if [ "$format" = "" ]
    then format=$MCDEFAULT_SQUEUE_FORMAT
  fi
  argstr=$(_build_argstr "$argstr" "$user" "-u");
  argstr=$(_build_argstr "$argstr" "$paritition" "-p");
  argstr=$(_build_argstr "$argstr" "$account" "-A");
  argstr=$(_build_argstr "$argstr" "$format" "--format");

  squeue $argstr
}

function run_on_node {
  local node="$1";
  if [ "$node" != "-*" ]
    then shift
  fi
  local account="$1";
  if [ "$account" != "-*" ]
    then shift
  fi
  local args="$@";
  local argstr;

  argstr=$(_build_argstr "$argstr" "$node" "-p")
  argstr=$(_build_argstr "$argstr" "$account" "-A")

  if [ "$arg_str" = "max_res" ]
    then arg_str="--mem=60gb --ntasks=8 --time=0-8:00:00 --pty /bin/bash"
  fi
  if [ "$arg_str" != "" ]
    then arg_str=" $arg_str"
  fi

  srun $arg_str

}

function interactive_node {
  run_on_node $@ --pty /bin/bash
}

function string_array_index {
  local i;
  local arr=($1);
  local value=$2;

  for i in "${!arr[@]}"; do
    if [[ "${arr[$i]}" = "${value}" ]]; 
      then echo "${i}"
      break;
    fi
  done
}

function get_job_id_by_name {
  local job_name=$1;
  local user=$2;
  local my_jobs;
  local job_index;
  local job_state;
  local job_id;

  user=$(_get_user "$user")

  my_jobs=$(squeue -u "$user" -h --format='%j %i')
  job_index=$(string_array_index "$my_jobs", "$job_name")

  if [ "$job_index" != "" ]; then
      my_jobs=($my_jobs)
      job_index=$((job_index+1))
      job_id=${my_jobs[$job_index]}
      echo $job_id
  fi
}

function get_job_node {
  local job_id=$1;
  local job_str;
  job_str=$(squeue -j $job_id -h --format='%N')
  if [ "$job_str" = "NODELIST" ]; then
    job_str=""
  fi

  echo "$job_str"
}

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
  servername=${server#*@}

  SOCKET_DIR=~/.ssh/connections
  mkdir -p $SOCKET_DIR
  socket="$SOCKET_DIR/$servername"
  if [ "$restart" ]; then
    rm "$socket" 2> /dev/null
  fi
  connected=$(_ssh_connected "$socket")
  conn_opt=$(_build_argstr "$conn_opt" "ControlPath=$socket")

  if [[ "$connected" == "false" ]]; then
    ssh -M -f -N $base_opts -o $conn_opt $server
  fi

  $cmd $base_opts -o $conn_opt ${args[@]}

}

function pssh {
  _ssh_like ssh $@
}
function psftp {
  _ssh_like sftp $@
}

DEFAULT_WAIT_FOR_JOB_RETRIES=5
DEFAULT_WAIT_FOR_JOB_PAUSE=1
WAIT_FOR_JOB_NODE_OPTS="S:R:"
function wait_for_job_node {
  local job_id=$(mcargs "$WAIT_FOR_JOB_NODE_OPTS" $@);
  local retries=$(mcoptvalue "$WAIT_FOR_JOB_NODE_OPTS" 'R' $@);
  local pause_time=$(mcoptvalue "$WAIT_FOR_JOB_NODE_OPTS" 'S' $@);
  local job_node;
  local i;

  if [ "$retries" = "" ];
    then retries="$DEFAULT_WAIT_FOR_JOB_RETRIES"
  fi
  if [ "$pause_time" = "" ]
    then pause_time="$DEFAULT_WAIT_FOR_JOB_PAUSE"
  fi

  if [ "$job_id" != "" ]; then
    for i in $(seq $retries); do
      job_node=$(get_job_node $job_id);
      if [ "$job_node" = "" ]; then
        sleep $pause_time
      fi
    done

    echo "$job_node"
  fi
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
      target_port=${spec_array[2]}
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

CONNECT_TO_JOB_OPTS="fnS:R:P:I:"
function connect_to_job {
  local job_id;
  local forwarding;
  local fwd;
  local wait_opts;
  local ssh_args;
  local ssh_opts;
  local post_args;
  local pause_time;

  forwarding=$(mcoptvalue "$CONNECT_TO_JOB_OPTS" "P" $@)
  if [ "$forwarding" != "" ]; then
    ssh_args=$(multi_fwd_spec "L" "$forwarding")
  fi

  job_id=($(mcargs "$CONNECT_TO_JOB_OPTS" $@))
  if [ "$job_id" = "" ]; 
    then echo "No job ID provided"
    else
      post_args="${job_id[@]:1}"
      job_id="${job_id[0]}"
      wait_opts=$(mcopts "$CONNECT_TO_JOB_OPTS" "f|n|P|H|I" $@)
      job_node=$(wait_for_job_node $wait_opts $job_id)
      if [ "$job_node" = "" ]
        then 
            echo "Timed out while waiting for job to start, canceling requested job"
            scancel $job_id
        else
            ssh_opts=$(mcopts "$CONNECT_TO_JOB_OPTS" "S|R|P|I" $@)
            ssh_args=$(_build_argstr "$ssh_args" "$ssh_opts")
            pause_time=$(mcoptvalue "$CONNECT_TO_JOB_OPTS" "I" $@)
            sleep $pause_time
            printf "%s\n" "ssh $ssh_args $job_node $post_args"
            ssh $ssh_args $job_node "$post_args"
      fi
  fi
}


function random_id {
  # thanks Stack Overflow
  local num_chars=$1;
  local rand_str;

  if [ "$num_chars" = "" ]
    then num_chars=13
  fi

  rand_str=$(tr -dc A-Za-z0-9 </dev/urandom | head -c $num_chars; echo);
  if [ "$rand_str" = "" ]
    then rand_str=$(openssl rand -base64 $num_chars)
  fi

  echo "$rand_str"
}

function slurm_job_setup() {
  # go to _real_ job dir just in case folder is messed up
  if [ -n $SLURM_JOB_ID ] ; then
  SLURM_JOB_SCRIPT=$(scontrol show job $SLURM_JOBID | awk -F= '/Command=/{print $2}')
  else
  SLURM_JOB_SCRIPT=$(realpath $0)
  fi
  SLURM_JOB_DIR=$(dirname $SLURM_JOB_SCRIPT)
  cd $SLURM_JOB_DIR

  if [ -n $SLURM_JOB_ID ] ; then
  SLURM_JOB_OUTPUT=$(scontrol show job $SLURM_JOBID | awk -F= '/StdOut=/{print $2}')
  else
  SLURM_JOB_OUTPUT="stdout"
  fi

    if [ -n $SLURM_JOB_ID ] ; then
  SLURM_JOB_TIME_LIMIT=$(echo $(scontrol show job $SLURM_JOBID | awk -F= '/TimeLimit=/{print $3}') | awk '{ print $1 }')
  else
  SLURM_JOB_TIME_LIMIT="N/A"
  fi

  echo
  echo "JOB: $SLURM_JOB_NAME"
  SLURM_JOB_START=$(date +%s.%N)
  echo "  START: $(date)"
  echo "    PWD: $PWD"
  echo "     ID: $SLURM_JOB_ID"
  echo "    OUT: $SLURM_JOB_OUTPUT"
  echo " SCRIPT: $SLURM_JOB_SCRIPT"
  echo "   TIME: $SLURM_JOB_TIME_LIMIT"
  echo "  NODES: $SLURM_JOB_NUM_NODES"
  echo "  PART.: $SLURM_JOB_PARTITION"
  echo "=============================================================================="

}

function slurm_job_cleanup() {

  local debug=$1;

  echo "=============================================================================="
  SLURM_JOB_END=$(date +%s.%N)
  SLURM_JOB_DIFF=$(echo "$SLURM_JOB_END - $SLURM_JOB_START" | bc)
  echo "   END: $(date)"
  echo "  TIME: $SLURM_JOB_DIFF"

  if [ "$debug" == "" ]; then debug=true ; fi
  if [ "$debug" != false ]; then

    echo
    echo
    echo
    echo
    echo
    echo
    echo "=============================DEBUG ENVIRONMENT================================"
    set

  fi

  # clean up after ourselves
  unset SLURM_JOB_TIME_LIMIT
  unset SLURM_JOB_DIR
  unset SLURM_JOB_SCRIPT
  unset SLURM_JOB_OUTPUT
  unset SLURM_JOB_START
  unset SLURM_JOB_END
  unset SLURM_JOB_DIFF

}

############################################################################
############################# ARG PARSE STUFF ##############################
############################################################################

# mcopts: EXTRACT OPTIONS
#     Takes a flag pattern and call signature
#     Returns the opts

function mcopts {

  local flag_pat;
  local ignore_pat;
  local opt_string;
  local opt_whitespace;
  local opt;
  local OPTARG;
  local OPTIND=1;

  flag_pat="$1";
  shift
  ignore_pat="$1";
  shift

  while getopts "$flag_pat" opt; do
    if [[ "$opt" =~ $ignore_pat ]]
      then
        :
      else
        if [ "$opt_string" != "" ]
          then opt_whitespace=" ";
          else opt_whitespace="";
        fi;
        if [ "$OPTARG" != "" ]
          then opt_string="$opt_string$opt_whitespace-$opt $OPTARG"
          else opt_string="$opt_string$opt_whitespace-$opt"
        fi
    fi
  done

  printf "%s" "$opt_string"

}

# mcoptvalue: EXTRACT OPTION VALUE
#     Takes a flag pattern, opt key, and call signature
#     Returns the opt value for the key

function mcoptvalue {

  local flag_pat;
  local value_pat;
  local opt;
  local opt_string;
  local opt_whitespace;
  local OPTARG;
  local OPTIND=1;

  flag_pat="$1";
  shift
  value_pat="$1";
  shift

  # while getopts ":$flag_pat:" opt; do
  #   case "$opt" in
  #     $value_pat)
  #       if [ "$opt_string" != "" ]
  #         then opt_whitespace=" ";
  #         else opt_whitespace="";
  #       fi;
  #       if [ "$OPTARG" = "" ]
  #         then OPTARG=true;
  #       fi
  #       opt_string="$opt_string$opt_whitespace$OPTARG"
  #       ;;
  #   esac;
  # done

  OPTIND=1;

  if [ "$opt_string" == "" ]; then
    while getopts "$flag_pat" opt; do
      case "$opt" in
        $value_pat)
          if [ "$opt_string" != "" ]
            then opt_whitespace=" ";
            else opt_whitespace="";
          fi;
          if [ "$OPTARG" = "" ]
            then OPTARG=true;
          fi
          opt_string="$opt_string$opt_whitespace$OPTARG"
          ;;
      esac;
    done
  fi

  printf "%s" "$opt_string"
}

# mcargs: EXTRACT ARGUMENTS
#     Takes a flag pattern and call signature
#     Returns just the args

function mcargs {

  local flag_pat;
  local opt;
  local OPTARG;
  local OPTIND;

  flag_pat="$1";
  shift

  while getopts "$flag_pat" opt; do
      :
  done
  shift "$((OPTIND -1))";

  printf "%s" "$*"

}