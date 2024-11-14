#! /bin/bash

if [ -f "$TUNNEL_DIR/configure_job.sh" ]
  then source $TUNNEL_DIR/configure_job.sh
  else source $HPCTUNNELS_DIR/configure_job.sh
fi

# Load in user-specified configuration
if [ -f "$TUNNEL_DIR/user.sh" ]; then
    source $TUNNEL_DIR/user.sh
fi

echo "Launching Jupyter on $PROCESS_PORT"
# Run JupyterLab on specified port

jupyter lab --port=$PROCESS_PORT --notebook-dir="/" --no-browser