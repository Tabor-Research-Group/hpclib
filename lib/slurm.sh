

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

function get_job_id_by_name {
  local job_name=$1;
  local user=$2;
  local my_jobs;
  local job_index;
  local job_state;
  local job_id;

  user=$(_get_user "$user")

  my_jobs=$(squeue -u "$user" -h --format='%j %i')
  job_index=$(string_array_index "$my_jobs" "$job_name")

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

CONNECT_TO_JOB_OPTS="46AaCfGgKkMNnqsTtVvXxYyfnb:B:c:e:E:L:l:i:J:F:D:o:O:Q:w:W:S:R:P:I:"
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
      wait_opts=$(mcoptsfrom "$CONNECT_TO_JOB_OPTS" "S|R" $@)
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

function slurm_job_script {
  local SLURM_JOB_SCRIPT

  if [ -n $SLURM_JOB_ID ] ; then
    SLURM_JOB_SCRIPT=$(scontrol show job $SLURM_JOB_ID | awk -F= '/Command=/{print $2}')
  else
    SLURM_JOB_SCRIPT=$(realpath $0)
  fi
  echo "$SLURM_JOB_SCRIPT"

}

function slurm_job_output {
  local SLURM_JOB_OUTPUT

  if [ -n $SLURM_JOB_ID ] ; then
    SLURM_JOB_OUTPUT=$(scontrol show job $SLURM_JOB_ID | awk -F= '/StdOut=/{print $2}')
  else
    SLURM_JOB_OUTPUT="stdout"
  fi

  echo "$SLURM_JOB_OUTPUT"

}

function slurm_job_time_limit {
  local slurm_job_time_limit

  if [ -n $SLURM_JOB_ID ] ; then
    SLURM_JOB_TIME_LIMIT=$(echo $(scontrol show job $SLURM_JOB_ID | awk -F= '/TimeLimit=/{print $3}') | awk '{ print $1 }')
  else
    SLURM_JOB_TIME_LIMIT="N/A"
  fi

}

function slurm_job_info() {
  # go to _real_ job dir just in case folder is messed up
  local SLURM_JOB_SCRIPT=$(slurm_job_script)
  local SLURM_JOB_DIR=$(dirname $SLURM_JOB_SCRIPT)
  local SLURM_JOB_OUTPUT=$(slurm_job_output)
  local SLURM_JOB_TIME_LIMIT=$(slurm_job_time_limit)

  cd $SLURM_JOB_DIR
  echo"===================================SLURM JOB==================================="
  echo "    JOB: $SLURM_JOB_NAME"
  echo "  START: $SLURM_JOB_START_TIME"
  echo "    PWD: $PWD"
  echo "     ID: $SLURM_JOB_ID"
  echo "    OUT: $SLURM_JOB_OUTPUT"
  echo " SCRIPT: $SLURM_JOB_SCRIPT"
  echo "   TIME: $SLURM_JOB_TIME_LIMIT"
  echo "  NODES: $SLURM_JOB_NUM_NODES"
  echo "  PART.: $SLURM_JOB_PARTITION"
  echo "=============================================================================="

}

function _slurm_cleanup {
  local CUR_DIR="$1"
  local WORK_DIR="$2"
  local RESULTS="$3"
  local results_list="$4"
  local exclude_list="$5"
  local rsync_list

  # move results of calc
  touch job_manager-complete
#    echo "Copying results back to ${RESULTS}"
  rsync_list="-av $results_list $exclude_list $WORK_DIR/ $RESULTS"
  # a workaround for some bash string encoding issue...
  eval "rsync $rsync_list"
#      cp $WORK_DIR/job_manager-complete $RESULTS/
#      cp $WORK_DIR/*.log $RESULTS/
#      cp $WORK_DIR/*.chk $RESULTS/
  echo 'Cleaning up'
  cd $CUR_DIR
  rm $CUR_DIR/$SLURM_JOB_ID
  rm -R $WORK_DIR

}

SLURM_COMMAND_SCRATCH_DIR="/tmp"
SLURM_COMMAND_DEFAULT_OUTPUT_NAME="output-%j.out"
SLURM_COMMAND_DEFAULT_JOB_FILES=""
SLURM_COMMAND_DEFAULT_OUTPUT_FILES="*.log,*.chk,*.out"
SLURM_COMMAND_EXECUTE_FLAGS="nO:J:H:S:R:E:W:"
function slurm_command_execute {
  local noscratch=$(mcoptvalue $SLURM_COMMAND_EXECUTE_FLAGS "n" $@)
  local SCRATCH=$(mcoptvalue $SLURM_COMMAND_EXECUTE_FLAGS "S" $@)
  local RESULTS=$(mcoptvalue $SLURM_COMMAND_EXECUTE_FLAGS "W" $@)
#  local input_file=$(mcoptvalue $SLURM_COMMAND_EXECUTE_FLAGS "I" $@)
  local output_file=$(mcoptvalue $SLURM_COMMAND_EXECUTE_FLAGS "O" $@)
  local job_list=$(mcoptvalue $SLURM_COMMAND_EXECUTE_FLAGS "J" $@)
  local job_exclude=$(mcoptvalue $SLURM_COMMAND_EXECUTE_FLAGS "H" $@)
  local results_list=$(mcoptvalue $SLURM_COMMAND_EXECUTE_FLAGS "R" $@)
  local exclude_list=$(mcoptvalue $SLURM_COMMAND_EXECUTE_FLAGS "E" $@)
  local args=$(mcargs $SLURM_COMMAND_EXECUTE_FLAGS $@)
  local cmd="${args[0]}"
  local input_file="${args[1]}"
  local rest="${args[2:]}"
  local CUR_DIR=$(pwd)
  local WORK_DIR
  local pid
  local rsync_list
  local time_cmd

  if [ -z "$cmd" ]; then
    echo "No command specified"
    return 1
  fi

  if [ -z "$SCRATCH" ]; then
    SCRATCH="$SLURM_COMMAND_SCRATCH_DIR"
  fi

  if [ -z "$RESULTS" ]; then
    RESULTS="$CUR_DIR"
  fi

  if [ -z "$output_file" ]; then
    if [ -z "$input_file" ];
      then output_file="$SLURM_COMMAND_DEFAULT_OUTPUT_NAME"
      else output_file="${filename%.*}-%j.out"
    fi
  fi
  output_file="${output_file//%j/$SLURM_JOB_ID}"

  WORK_DIR="$SCRATCH/$SLURM_JOB_ID"


  if [ -z "$job_list" ]; then
    job_list="$SLURM_COMMAND_DEFAULT_JOB_FILES"
  fi
  if [ -n "$job_list" ]; then
    job_list="--include=${job_list//,/ --include=}"
  fi
  if [ -n "$job_exclude" ]; then
    job_exclude="--exclude=${job_exclude//,/ --exclude=}"
  fi

  if [ -z "$results_list" ]; then
    results_list="$SLURM_COMMAND_DEFAULT_OUTPUT_FILES"
  fi
  if [ -n "$results_list" ]; then
    results_list="--include=${results_list//,/ --include=}"
  fi

  if [ -n "$exclude_list" ]; then
    exclude_list="--exclude=${exclude_list//,/ --exclude=}"
  fi

  echo "### LAUCNHING ###"
  echo "    COMMAND: $cmd"
  echo " INPUT FILE: $input_file"
  echo "   OUT FILE: $output_file"
  echo "WORKING DIR: $WORK_DIR"
  echo "RESULTS DIR: $RESULTS"
  echo "SYNC. FILES: $job_list"
  echo "EXCL. FILES: $job_exclude"
  echo " RES. FILES: $results_list"
  echo " RES. EXCL.: $exclude_list"
  if [ -d $SCRATCH ] && [ -z "$noscratch" ]; then
    #setup scratch dirs
    clock=`date +%s%15N`
    WORK_DIR=$SCRATCH/$SLURM_JOB_ID
    echo "Setting up directories"
    echo "  syncing: -a $job_list $job_exclude $CUR_DIR/ $WORK_DIR"

    job_exclude=($job_exclude)
    job_list=($job_list)

    mkdir -p $WORK_DIR
    rsync_list="-av ${job_list[@]} ${job_exclude[@]} $CUR_DIR/ $WORK_DIR"
    # a workaround for some bash string encoding issue...
    eval "rsync $rsync_list"
    ln -s $WORK_DIR $CUR_DIR/$SLURM_JOB_ID
    cd $WORK_DIR

    trap 'kill -9 $pid; _slurm_cleanup "$CUR_DIR" "$WORK_DIR" "$RESULTS" "$results_list" "$exclude_list" ; exit' SIGTERM SIGINT

    if [ -n "$input_file" ];
      then echo "Running: $cmd \"$input_file\""
      else echo "Running: $cmd"
    fi
    echo "Writing to: \"$output_file\""

    if [ -n "$input_file" ]; then
      input_file="\"$input_file\""
    fi
    time_cmd="time -p $cmd "$input_file"  > \"$output_file\" & pid=\$!;"
#    echo "$time_cmd"
    {
        # to allow commands to escape strings properly
        eval "$time_cmd"
    } 2>> "$output_file"

    wait
    _slurm_cleanup "$CUR_DIR" "$WORK_DIR" "$RESULTS" "$results_list" "$exclude_list"
  else

    if [ -n "$input_file" ];
      then echo "Running: $cmd \"$input_file\""
      else echo "Running: $cmd"
    fi
    echo "Writing to: \"$output_file\""

    if [ -n "$input_file" ]; then
      input_file="\"$input_file\""
    fi
    time_cmd="time -p $cmd "$input_file" > \"$output_file\" & pid=\$!;"
#    echo "$time_cmd"
    {
        # to allow commands to escape strings properly
        eval "$time_cmd"
    } 2>> "$output_file"

    wait
  fi

}

function slurm_job_footer() {

  local debug=$1;
  local SLURM_JOB_END=$(date +%s.%N)
  local SLURM_JOB_DIFF=$(echo "$SLURM_JOB_END - $SLURM_JOB_START_TIME" | bc)

  echo "=============================================================================="
  echo "   END: $(date)"
  echo "  TIME: $SLURM_JOB_DIFF"

  if [ -n "$debug" ]; then

    echo
    echo
    echo
    echo
    echo
    echo
    echo "=============================DEBUG ENVIRONMENT================================"
    set

  fi

#  # clean up after ourselves
#  unset SLURM_JOB_TIME_LIMIT
#  unset SLURM_JOB_DIR
#  unset SLURM_JOB_SCRIPT
#  unset SLURM_JOB_OUTPUT
#  unset SLURM_JOB_START
#  unset SLURM_JOB_END
#  unset SLURM_JOB_DIFF

}

SLURM_COMMAND_DEFAULT_TIME=5:00:00
SLURM_COMMAND_DEFAULT_MEM=10gb
function submit_slurm_job() {
  local scratch_dir="$(mclongoptvalue scratch-dir $@)"
  local result_dir="$(mclongoptvalue result-dir $@)"
  local output_file="$(mclongoptvalue output $@)"
  local job_files="$(mclongoptvalue job-inputs $@)"
  local job_exclude="$(mclongoptvalue job-exclude $@)"
  local result_files="$(mclongoptvalue results $@)"
  local result_exclude="$(mclongoptvalue result-exclude $@)"
  local time="$(mclongoptvalue time $@)"
  local mem="$(mclongoptvalue mem $@)"
  local args=($(mclongargs $@))
  local command=${args[0]}
  local input_file=${args[1]}
  local job_name
  local export_args

#  local scratch_dir="$(mclongoptvalue scratch-dir $@)";

  if [ -z "$mem" ]; then
    mem="$SLURM_COMMAND_DEFAULT_MEM"
  fi

  if [ -z "$time" ]; then
    time="$SLURM_COMMAND_DEFAULT_TIME"
  fi

  job_name="${input_file%.*}"
  export_args="SCRATCH='$scratch_dir',RESULTS='$result_dir',INPUT_FILE='$input_file',OUTPUT_FILE='$output_file',
JOB_FILES='$job_files',JOB_EXCLUDE='$job_exclude',RESULT_FILES='$result_files',RESULT_EXCLUDE='$result_exclude'"
  sbatch \
    --export="$export_args" \
    --time=$time --mem=$mem --job-name=${job_name} --out=${job_name}-%j.out\
    $HPCLIB_DIR/templates/sbatch_core.sh \
    $command \
    ${args[@]:2}
}