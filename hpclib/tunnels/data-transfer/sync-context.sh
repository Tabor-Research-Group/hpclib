#!/bin/sh
# The server supplies its trusted action_paths.py location; use the sibling shared sync implementation.
set -eu
helper=$1
shift
exec python3 "$(dirname "$helper")/sync_context.py" "$@"
