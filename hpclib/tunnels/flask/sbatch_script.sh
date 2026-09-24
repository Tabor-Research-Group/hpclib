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

# Supply an import path after -- (for example mypackage.web:app), or
# export FLASK_APP through the tunnel's environment.
flask_app="${1:-${FLASK_APP:-}}"
if [ -z "$flask_app" ]; then
  echo 'flask tunnel: provide an app after -- or set FLASK_APP' >&2
  exit 2
fi
if [ "$#" -gt 0 ]; then
  shift
fi

echo "Launching Flask app $flask_app on 127.0.0.1:$PROCESS_PORT"
exec flask --app "$flask_app" run "$@" --host=127.0.0.1 --port="$PROCESS_PORT" --no-reload --no-debugger
