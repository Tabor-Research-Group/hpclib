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
singularity run $VSCODE_CONTAINER --bind-addr localhost:$PROCESS_PORT
echo "Exited singularity"