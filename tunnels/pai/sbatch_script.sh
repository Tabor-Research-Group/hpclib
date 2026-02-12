#! /bin/bash

if [ -f "$TUNNEL_DIR/configure_job.sh" ]
  then source $TUNNEL_DIR/configure_job.sh
  else source $HPCTUNNELS_DIR/configure_job.sh
fi

# Load in user-specified configuration
if [ -f "$TUNNEL_DIR/user.sh" ]; then
    source $TUNNEL_DIR/user.sh
fi

echo "Launching database connection on $PROCESS_PORT"
# Run JupyterLab on specified port

export PAI_PORT=$PROCESS_PORT
cd $PAI_ROOT_DIR && \
  bash singularity_compose.sh