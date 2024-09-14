#! /bin/bash

if [ -f "$TUNNEL_DIR/configure_job.sh" ]
  then source $TUNNEL_DIR/configure_job.sh
  else source $HPCTUNNELS_DIR/configure_job.sh
fi

# Load in user-specified configuration
if [ -f "$TUNNEL_DIR/user.sh" ]; then
    source $TUNNEL_DIR/user.sh
fi

# Run VS Code container with singularity on default port
set -a
mkdir -p /scratch/user/$VSCODE_USER/.config
echo "In VS Code terminals, start by running `source $TUNNEL_ENV_FIlE`"
echo "If necessary, to load the same conda env, use `conda activate $CONDA_ENVIRONMENT`"
singularity run $VSCODE_CONTAINER
echo "Exited singularity"