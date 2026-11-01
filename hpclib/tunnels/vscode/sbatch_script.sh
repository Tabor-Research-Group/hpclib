#! /bin/bash

if [ -f "$TUNNEL_DIR/configure_job.sh" ]
  then source $TUNNEL_DIR/configure_job.sh
  else source $HPCTUNNELS_DIR/configure_job.sh
fi

# Load in user-specified configuration
if [ -f "$TUNNEL_DIR/user.sh" ]; then
    source $TUNNEL_DIR/user.sh
fi

echo "mounting directories from $VSCODE_BIND_PATHS"
echo "launching from $VSCODE_ROOT_DIR"
echo "keeping extensions and settings in $VSCODE_DATA_DIR"

# Run VS Code container with singularity on default port
cd "$VSCODE_ROOT_DIR"
mkdir -p "$VSCODE_ROOT_DIR/.config" "$VSCODE_DATA_DIR/user-data" "$VSCODE_DATA_DIR/extensions"

# The container sees the config directory, your bind list, and the data directory at its own path (unless a bind in the
# list already covers it)
binds="$VSCODE_ROOT_DIR/.config:/home/coder/.config"
[ -z "$VSCODE_BIND_PATHS" ] || binds="$binds,$VSCODE_BIND_PATHS"
covered=false
IFS=, read -r -a pairs <<< "$VSCODE_BIND_PATHS"
for pair in "${pairs[@]}"; do
  dest="${pair#*:}"; dest="${dest%%:*}"
  case "$VSCODE_DATA_DIR/" in "${dest%/}"/*) covered=true ;; esac
done
[ "$covered" = true ] || binds="$binds,$VSCODE_DATA_DIR:$VSCODE_DATA_DIR"

args=(--bind-addr "127.0.0.1:$PROCESS_PORT"
      --user-data-dir "$VSCODE_DATA_DIR/user-data" --extensions-dir "$VSCODE_DATA_DIR/extensions")
if [ -n "$VSCODE_START_FOLDER" ]; then
  echo "opening $VSCODE_START_FOLDER"
  args+=("$VSCODE_START_FOLDER")
fi
echo "singularity run --bind $binds $VSCODE_CONTAINER ${args[*]}"
singularity run --bind "$binds" "$VSCODE_CONTAINER" "${args[@]}"
echo "Exited singularity"
