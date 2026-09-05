#! /bin/bash

if [ -f "$TUNNEL_DIR/configure_job.sh" ]
  then source $TUNNEL_DIR/configure_job.sh
  else source $HPCTUNNELS_DIR/configure_job.sh
fi

# Load in user-specified configuration
if [ -f "$TUNNEL_DIR/user.sh" ]; then
    source $TUNNEL_DIR/user.sh
fi

# Run JupyterLab on specified port
cd $TUNNEL_DIR
python -m mdsrv_start --port=$PROCESS_PORT