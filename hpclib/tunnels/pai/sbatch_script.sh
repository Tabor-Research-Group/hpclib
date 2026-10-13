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
# singularity-compose.sh makes nginx listen on PAI_LOCAL_BIND_TARGET (127.0.0.1:PORT unless this is set)
export PAI_LOCAL_BIND_TARGET="${PAI_LOCAL_BIND_TARGET:-$PAI_BIND_HOST:$PROCESS_PORT}"

# Optionally announce where the database listens, so jobs on any node can find it (see PAI_BIND_HOST).
pai_announce_file=""
pai_announce_pid=""
pai_announce() {
  local url="http://$(hostname -s):$PROCESS_PORT" now
  now=$(date +%s)
  printf '{"url": "%s", "name": "pai-%s", "host": "%s", "pid": %s, "ttl": 300.0, "announced_at": %s, "tags": {"job": "%s", "bind": "%s"}}\n' \
    "$url" "$(hostname -s)" "$(hostname -s)" "$$" "$now" "${SLURM_JOB_ID:-}" "$PAI_BIND_HOST" > "$pai_announce_file.tmp" \
    && mv -f "$pai_announce_file.tmp" "$pai_announce_file"
}
if [ -n "$PAI_ANNOUNCE_DIR" ]; then
  case "$PAI_BIND_HOST" in
    127.*|localhost|::1)
      echo "PAI_ANNOUNCE_DIR is set but PAI_BIND_HOST=$PAI_BIND_HOST is loopback: not announcing (other nodes cannot use it)" >&2 ;;
    *)
      mkdir -p "$PAI_ANNOUNCE_DIR" && pai_announce_file="$PAI_ANNOUNCE_DIR/pai-$(hostname -s).json"
      if [ -n "$pai_announce_file" ]; then
        pai_announce
        ( while sleep 60; do pai_announce; done ) &
        pai_announce_pid=$!
        echo "Announcing $pai_announce_file (bind $PAI_LOCAL_BIND_TARGET)"
      fi ;;
  esac
fi
trap 'tunnel_unregister_instance pai; [ -n "$pai_announce_pid" ] && kill "$pai_announce_pid" 2>/dev/null; [ -n "$pai_announce_file" ] && rm -f "$pai_announce_file" "$pai_announce_file.tmp"' EXIT

echo "Launching the PAI database on port $PROCESS_PORT ($(hostname -s), job $SLURM_JOB_ID;" \
  "source $([ "$PAI_BIND_SOURCE" = 0 ] && echo "from the images" || echo "bound from $PAI_ROOT_DIR/proto-auto-interface"))"
cd "$PAI_ROOT_DIR/proto-auto-interface" && \
  bash singularity-compose.sh
