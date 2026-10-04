#! /bin/bash

if [ -f "$TUNNEL_DIR/configure_job.sh" ]
  then source $TUNNEL_DIR/configure_job.sh
  else source $HPCTUNNELS_DIR/configure_job.sh
fi

# Load in user-specified configuration
if [ -f "$TUNNEL_DIR/user.sh" ]; then
    source $TUNNEL_DIR/user.sh
fi

# The database is shared: tunnels started while this job runs attach to it (SHARED_INSTANCE in
# tunnel_config.sh) instead of starting another. Its port is PROCESS_PORT unless something on this node
# already has that one; either way it is registered, so the tunnels know where to connect.
PROCESS_PORT=$(tunnel_pick_port "$PROCESS_PORT")
export PAI_PORT=$PROCESS_PORT
tunnel_register_instance pai "$PROCESS_PORT"
trap 'tunnel_unregister_instance pai' EXIT

echo "Launching the PAI database on port $PROCESS_PORT ($(hostname -s), job $SLURM_JOB_ID)"
cd "$PAI_ROOT_DIR/proto-auto-interface" && \
  bash singularity-compose.sh
