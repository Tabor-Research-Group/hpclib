#!/bin/bash
# Runs on the login node (RUN_ON_LOGIN_NODE=true), as a child of the tunnel, on its forwarded port.
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$here/user.sh" ] && source "$here/user.sh"
if ! "$FSDIAG_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 6))' 2> /dev/null; then
  echo "fsdiag: $FSDIAG_PYTHON isn't Python 3.6 or newer; set FSDIAG_PYTHON in the app's settings" >&2
  exit 2
fi
exec "$FSDIAG_PYTHON" -u "$here/fsdiag_server.py" --port "$PROCESS_PORT" "$@"
