#! /bin/bash

if [ -f "$TUNNEL_DIR/configure_job.sh" ]
  then source $TUNNEL_DIR/configure_job.sh
  else source $HPCTUNNELS_DIR/configure_job.sh
fi

# Load in user-specified configuration
if [ -f "$TUNNEL_DIR/user.sh" ]; then
    source $TUNNEL_DIR/user.sh
fi

echo "mounting directories from $VSCODE_BIND_PATHS"
echo "launching from $VSCODE_ROOT_DIR"

# Run VS Code container with singularity on default port
cd $VSCODE_ROOT_DIR
set -a
mkdir -p $VSCODE_ROOT_DIR/.config
echo "singularity run \
  --bind $VSCODE_ROOT_DIR/.config:/home/coder/.config,$VSCODE_BIND_PATHS \
  $VSCODE_CONTAINER \
    --bind-addr localhost:$PROCESS_PORT"
singularity run \
  --bind $VSCODE_ROOT_DIR/.config:/home/coder/.config,$VSCODE_BIND_PATHS \
  $VSCODE_CONTAINER \
    --bind-addr localhost:$PROCESS_PORT
echo "Exited singularity"