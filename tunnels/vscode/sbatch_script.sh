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

# Run VS Code container with singularity on default port
mkdir -p /scratch/user/$VSCODE_USER/.config
singularity run $VSCODE_CONTAINER
echo "Exited singularity"