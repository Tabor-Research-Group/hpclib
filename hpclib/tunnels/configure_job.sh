#! /bin/bash

. ~/.bashrc

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
if [ "$START_SLURM_SERVER" = "true" ]; then
  export SLURM_SOCKET_PORT=$(random_port 10000 65535)
  python $HPCSERVERS_DIR/slurm_server.py &
fi