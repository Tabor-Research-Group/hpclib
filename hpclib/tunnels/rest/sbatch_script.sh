#!/usr/bin/env bash
set -e

if [ -f "$TUNNEL_DIR/configure_job.sh" ]; then
  source "$TUNNEL_DIR/configure_job.sh"
else
  source "$HPCTUNNELS_DIR/configure_job.sh"
fi

if [ -f "$TUNNEL_DIR/user.sh" ]; then
  source "$TUNNEL_DIR/user.sh"
fi

# Arguments after -- go straight to rest_server.py, for example
#   -- --allow /scratch/user/me/project --allow /scratch/user/me/data
# HPC_REST_ALLOWED_DIRS (colon-separated) and HPC_REST_TOKEN_FILE can be
# set through --env instead. The server only needs the standard library.
echo "Launching hpclib REST server on 127.0.0.1:$PROCESS_PORT"
exec "${HPC_REST_PYTHON:-python3}" "$HPCSERVERS_DIR/rest_server.py" \
  --host 127.0.0.1 --port "$PROCESS_PORT" "$@"
