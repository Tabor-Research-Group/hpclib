#! /bin/bash

. ~/.bashrc
# tunnel_register_instance, tunnel_pick_port, ... (for jobs that share their service or pick their port)
if [ -f "${BASH_SOURCE[0]%/*}/instances.sh" ]; then
  . "${BASH_SOURCE[0]%/*}/instances.sh"
fi

if [ "$ENABLE_WEB_PROXY" = "true" ]; then
  module load WebProxy
fi
# The tunnel's conda environment, if it has one and conda is available. Set
# CONDA_MODULE (e.g. in ~/.local/tunnels/config.sh) to load conda from a module
# where it isn't on PATH by default; REQUIRE_CONDA=true makes a missing conda
# an error instead of a warning.
if [ -n "$CONDA_ENVIRONMENT" ]; then
  if ! type conda > /dev/null 2>&1 && [ -n "${CONDA_MODULE:-}" ]; then
    module load $CONDA_MODULE || echo "hpclib: could not load $CONDA_MODULE for conda" >&2
  fi
  if type conda > /dev/null 2>&1; then
    # a conda from a module is a plain program until its shell hook is loaded
    if [ "$(type -t conda)" != function ]; then
      eval "$(conda shell.bash hook 2> /dev/null)" || true
    fi
    conda activate "$CONDA_ENVIRONMENT" ||
      echo "hpclib: could not activate the conda environment '$CONDA_ENVIRONMENT'; continuing without it" >&2
  elif [ "${REQUIRE_CONDA:-false}" = true ]; then
    echo "hpclib: this tunnel needs conda (environment '$CONDA_ENVIRONMENT'), and conda isn't available;" \
      "set CONDA_MODULE in ~/.local/tunnels/config.sh to load it from a module" >&2
    exit 1
  else
    echo "hpclib: conda isn't available, so the '$CONDA_ENVIRONMENT' environment isn't activated" \
      "(set CONDA_MODULE in ~/.local/tunnels/config.sh to load conda from a module)" >&2
  fi
fi
# --link=push: this job connects back to the login node that started it with ssh -R (the login node can't ssh here).
# It forwards the login node's port or socket (what your machine's forward reaches) to this service, and keeps at it:
# while the waiting page still holds a TCP port the first tries fail (ExitOnForwardFailure), then one succeeds.
# The compute-node key is the one connect_to_job uses (setup_cluster_ssh; HPCLIB_COMPUTE_IDENTITY_FILE).
hpclib_push_forward() {
  local bind target identity
  local -a opts=(-N -T -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=4
                 -o StreamLocalBindUnlink=yes)
  if [ "${HPCLIB_LOGIN_ENDPOINT:-port}" = socket ]; then bind="$HPCLIB_LOGIN_SOCKET"; else bind="127.0.0.1:$HPCLIB_LOGIN_PORT"; fi
  if [ "${HPCLIB_JOB_ENDPOINT:-port}" = socket ]; then
    target="${HPCLIB_JOB_SOCKET_DIR:-/tmp}/hpclib-$(id -un)-${SLURM_JOB_ID:-$$}.sock"
  else
    target="127.0.0.1:$PROCESS_PORT"
  fi
  identity="${HPCLIB_COMPUTE_IDENTITY_FILE-$HOME/.ssh/id_ed25519_hpclib_compute}"
  if [ -n "$identity" ] && [ -f "$identity" ]; then opts+=(-i "$identity" -o IdentitiesOnly=yes); fi
  if [ "${HPCLIB_COMPUTE_HOST_KEYS:-ask}" = accept-new ]; then opts+=(-o StrictHostKeyChecking=accept-new); fi
  echo "hpclib: connecting back to $HPCLIB_LOGIN_HOST: ssh -R $bind:$target"
  (
    while :; do
      ssh "${opts[@]}" -R "$bind:$target" "$HPCLIB_LOGIN_HOST" || echo "hpclib: ssh to $HPCLIB_LOGIN_HOST ended ($?); trying again" >&2
      sleep "${HPCLIB_PUSH_RETRY:-3}"
    done
  ) &
}
if [ "${HPCLIB_TUNNEL_LINK:-pull}" = push ] && [ -n "${SLURM_JOB_ID:-}" ] && [ "${TUNNEL_ON_LOGIN_NODE:-false}" != true ]; then
  hpclib_push_forward
fi

if [ "$START_SLURM_SERVER" = "true" ]; then
  export SLURM_SOCKET_PORT=$(random_port 10000 65535)
  python $HPCSERVERS_DIR/slurm_server.py &
fi