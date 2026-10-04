#!/bin/bash
# Running instances of a tunnel's service, so that a tunnel can attach to one another job already runs instead
# of starting its own (SHARED_INSTANCE=true in tunnel_config.sh), and so that a job can tell its tunnel which
# port it took (PROCESS_PORT_FROM_JOB=true). Each instance is a file
#
#   $HPCTUNNELS_DATA_DIR/instances/TUNNEL/JOBID      containing "NODE PORT"
#
# that the job writes once its service listens (tunnel_register_instance) and removes when it ends; a file whose
# job has left the queue is dropped the next time anyone looks. Sourced by start_tunnel.sh, configure_job.sh
# (so sbatch scripts have these) and setup_tunnel.sh.

_tunnel_instances_dir() {  # _tunnel_instances_dir TUNNEL
  printf '%s/instances/%s\n' "${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}" "$1"
}

# In a job: record that this job serves TUNNEL on PORT of this node.
tunnel_register_instance() {  # tunnel_register_instance TUNNEL PORT
  local dir
  if [ -z "${SLURM_JOB_ID:-}" ]; then
    echo "tunnel_register_instance: not in a SLURM job" >&2
    return 1
  fi
  dir=$(_tunnel_instances_dir "$1")
  mkdir -p "$dir" || return 1
  printf '%s %s\n' "$(hostname -s)" "$2" > "$dir/.$SLURM_JOB_ID.tmp" && mv -f "$dir/.$SLURM_JOB_ID.tmp" "$dir/$SLURM_JOB_ID"
}

tunnel_unregister_instance() {  # tunnel_unregister_instance TUNNEL
  [ -n "${SLURM_JOB_ID:-}" ] && rm -f "$(_tunnel_instances_dir "$1")/$SLURM_JOB_ID"
}

# The port JOB registered for TUNNEL, once it has.
tunnel_instance_port() {  # tunnel_instance_port TUNNEL JOB
  local file node port
  file="$(_tunnel_instances_dir "$1")/$2"
  [ -f "$file" ] || return 1
  read -r node port < "$file"
  case "$port" in ''|*[!0-9]*) return 1 ;; esac
  printf '%s\n' "$port"
}

# "JOB NODE PORT" of a running instance of TUNNEL, the newest first. Files of jobs that have left the queue are
# removed; when squeue can't be reached nothing is removed or reported.
tunnel_find_instance() {  # tunnel_find_instance TUNNEL
  local dir file job node port state out
  dir=$(_tunnel_instances_dir "$1")
  [ -d "$dir" ] || return 1
  for file in $(ls -t "$dir" 2>/dev/null); do
    case "$file" in ''|*[!0-9]*) continue ;; esac
    job="$file"
    if ! out=$(squeue -h -j "$job" -o %T 2>&1); then
      if printf '%s' "$out" | grep -qi "invalid job id"; then rm -f "$dir/$job"; fi
      continue
    fi
    state=$(printf '%s\n' "$out" | head -n 1)
    if [ -z "$state" ]; then
      rm -f "$dir/$job"
    elif [ "$state" = RUNNING ]; then
      read -r node port < "$dir/$job"
      case "$port" in ''|*[!0-9]*) continue ;; esac
      printf '%s %s %s\n' "$job" "$node" "$port"
      return 0
    fi
  done
  return 1
}

# In a job: PREFERRED if nothing listens on it on this node, else a free port.
tunnel_pick_port() {  # tunnel_pick_port PREFERRED
  local preferred="$1" port
  if command -v python3 > /dev/null 2>&1; then
    python3 - "$preferred" <<'PY'
import socket, sys
def free(port):
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return s.getsockname()[1]
    except OSError:
        return None
    finally:
        s.close()
print(free(int(sys.argv[1] or 0)) or free(0))
PY
    return
  fi
  for port in "$preferred" $(shuf -i 20000-60000 -n 20); do
    if ! (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      printf '%s\n' "$port"
      return 0
    fi
  done
  return 1
}
