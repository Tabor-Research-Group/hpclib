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
# The Python is the launcher setup_agents writes (~/.local/tunnels/rest/python,
# which loads the Python module it found), unless HPC_REST_PYTHON names one.
rest_python="${HPC_REST_PYTHON:-}"
if [ -z "$rest_python" ]; then
  rest_python="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/rest/python"
  [ -x "$rest_python" ] || rest_python=python3
fi
echo "Launching hpclib REST server on 127.0.0.1:$PROCESS_PORT (Python: $rest_python)"
exec "$rest_python" "$HPCSERVERS_DIR/rest_server.py" \
  --host 127.0.0.1 --port "$PROCESS_PORT" "$@"
