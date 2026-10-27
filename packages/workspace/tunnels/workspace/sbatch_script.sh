#!/bin/bash
# Runs on the login node (RUN_ON_LOGIN_NODE=true), as a child of the tunnel, on its forwarded port.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$here/user.sh" ] && source "$here/user.sh"
if ! "$WORKSPACE_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 6))' 2> /dev/null; then
  echo "workspace: $WORKSPACE_PYTHON isn't Python 3.6 or newer; set WORKSPACE_PYTHON in the Workspace's settings" >&2
  exit 2
fi
exec "$WORKSPACE_PYTHON" -u "$here/workspace_server.py" --port "$PROCESS_PORT" "$@"
