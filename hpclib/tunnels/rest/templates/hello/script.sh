#!/bin/bash
echo "job $SLURM_JOB_ID on $(hostname): $HPC_PARAM_MESSAGE"
sleep "$HPC_PARAM_SLEEP_SECONDS"
