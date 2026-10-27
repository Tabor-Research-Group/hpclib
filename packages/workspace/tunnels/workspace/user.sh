# Defaults for the Workspace's settings; the saved settings (~/.local/tunnels/settings/workspace.sh) win.
export WORKSPACE_PYTHON="${WORKSPACE_PYTHON:-python3}"
export WORKSPACE_ROOT="${WORKSPACE_ROOT:-}"                       # where it opens; empty: your home directory
export WORKSPACE_EDIT_LIMIT_MB="${WORKSPACE_EDIT_LIMIT_MB:-800}"  # the largest file the editor opens
export WORKSPACE_CONFIRM_MB="${WORKSPACE_CONFIRM_MB:-20}"         # above this, it asks before loading one
export WORKSPACE_CDN="${WORKSPACE_CDN:-https://cdn.jsdelivr.net/npm}"   # where CodeMirror and xterm.js load from
