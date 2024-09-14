#! /bin/bash

# General setup, probably doesn't need to be changed
. ~/bashrc
module load WebProxy
conda activate $CONDA_ENVIRONMENT
python $HPCSERVERS_DIR/slurm_server.py &

# Load in user-specified configuration
if [ -f "$TUNNEL_DIR/user.sh"]; then
    source $TUNNEL_DIR/user.sh
fi

# Run JupyterLab on specified port
jupyter lab --port=$port --no-browser