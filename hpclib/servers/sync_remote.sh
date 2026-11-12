#!/bin/sh
# Versioned JSON protocol on stdin/stdout; Git diagnostics stay in the JSON error response.
set -eu
exec python3 "$(dirname "$0")/sync_git.py" "$@"
