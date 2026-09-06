#!/bin/bash
#SBATCH --time=00:03:00 
#SBATCH --mem=100M 
#SBATCH --signal=B:TERM@30 
#SBATCH --ntasks=1

# Set up the environment
. "$SCRATCH/hpclib/hpclib.sh"
conda activate default
module load Gaussian/g16_C01

# Dispatch to python-side driver script
CONFIG_FILE="$1"

function submit {
    python driver.py submit $CONFIG_FILE
}

function restart {
    python driver.py restart $CONFIG_FILE
}

# Manage running/restarting the job
job_queue_run