#!/bin/bash

COMMAND="$1"
shift

if [ -z "$RESULTS" ]; then
  RESULTS=$(pwd)
fi

if [ -z "$SCRATCH" ]; then
  SCRATCH=/scratch/user/`whoami`
fi

# Settings
if [ -z "$INPUT_FILE" ]; then
  if [ -z "$EXT" ]; then
    case "$COMMAND" in
      python*)
        EXT=".py"
        ;;
      g09)
        EXT=".gjf"
        ;;
      g16)
        EXT=".gjf"
        ;;
    esac
  fi
  if [ -n "$EXT" ];
    then
      INPUT_FILE="${SLURM_JOB_NAME%.*}$EXT"
    else
      INPUT_FILE="$SLURM_JOB_NAME"
  fi
fi

# OUTPUT_FILE="${SLURM_JOB_NAME%.*}.log"

if [ -n "$ENV" ]; then
  . ~/.bashrc
  conda activate $ENV
fi

. ~/hpclib/hpclib.sh
if [ -n "$NOSCRATCH" ]; then
    argstr="-n"
  else
    argstr=""
fi
argstr=$(_build_argstr "$argstr" "$SCRATCH" "-S")
argstr=$(_build_argstr "$argstr" "$RESULTS" "-W")
argstr=$(_build_argstr "$argstr" "$OUTPUT_FILE" "-O")
argstr=$(_build_argstr "$argstr" "$JOB_FILES" "-J")
argstr=$(_build_argstr "$argstr" "$JOB_EXCLUDE" "-H")
argstr=$(_build_argstr "$argstr" "$RESULT_FILES" "-R")
argstr=$(_build_argstr "$argstr" "$RESULT_EXCLUDE" "-E")

slurm_job_info
echo "EXECUTING: $argstr $COMMAND $INPUT_FILE $@"
slurm_command_execute $argstr $COMMAND $INPUT_FILE $@
slurm_job_footer "$DEBUG"