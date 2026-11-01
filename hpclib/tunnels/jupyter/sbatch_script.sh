#! /bin/bash

if [ -f "$TUNNEL_DIR/configure_job.sh" ]
  then source $TUNNEL_DIR/configure_job.sh
  else source $HPCTUNNELS_DIR/configure_job.sh
fi

# Load in user-specified configuration
if [ -f "$TUNNEL_DIR/user.sh" ]; then
    source $TUNNEL_DIR/user.sh
fi

# Where jupyter comes from, besides the tunnel's conda environment (the console's JupyterLab settings set these):
#   HPCLIB_JUPYTER_MODULES  modules to load, colon-separated (e.g. GCCcore/13.2.0:JupyterLab/4.2.0)
#   HPCLIB_JUPYTER_PROJECT  a uv or pixi project whose environment has jupyterlab (.venv or .pixi/envs/default)
if [ -n "${HPCLIB_JUPYTER_MODULES:-}" ]; then
  module load $(printf '%s' "$HPCLIB_JUPYTER_MODULES" | tr ':' ' ') ||
    echo "hpclib: could not load the modules $HPCLIB_JUPYTER_MODULES" >&2
fi
if [ -n "${HPCLIB_JUPYTER_PROJECT:-}" ]; then
  found=''
  for env in "$HPCLIB_JUPYTER_PROJECT/.venv" "$HPCLIB_JUPYTER_PROJECT/.pixi/envs/default"; do
    if [ -x "$env/bin/jupyter" ]; then
      export PATH="$env/bin:$PATH"
      case "$env" in
        */.venv) export VIRTUAL_ENV="$env" ;;
        *) export CONDA_PREFIX="$env"
           for f in "$env"/etc/conda/activate.d/*.sh; do [ -f "$f" ] && . "$f"; done ;;
      esac
      found="$env"
      break
    fi
  done
  [ -n "$found" ] || echo "hpclib: $HPCLIB_JUPYTER_PROJECT has no environment with jupyter in it" \
    "(.venv or .pixi/envs/default); add jupyterlab to the project and sync it" >&2
fi
if ! command -v jupyter > /dev/null 2>&1; then
  echo "hpclib: jupyter isn't available in this job: install jupyterlab in the conda environment" \
    "'${CONDA_ENVIRONMENT:-}', or set a module or a uv/pixi project in the console's JupyterLab settings" >&2
  exit 1
fi

# Where Jupyter keeps its kernels, extensions and settings, if not in your home directory (the console's JupyterLab
# settings set these), and the folder the file browser starts in (default /, the whole file system)
if [ -n "${HPCLIB_JUPYTER_DATA_DIR:-}" ]; then
  mkdir -p "$HPCLIB_JUPYTER_DATA_DIR" && export JUPYTER_DATA_DIR="$HPCLIB_JUPYTER_DATA_DIR" ||
    echo "hpclib: could not create $HPCLIB_JUPYTER_DATA_DIR; using Jupyter's default data directory" >&2
fi
if [ -n "${HPCLIB_JUPYTER_CONFIG_DIR:-}" ]; then
  mkdir -p "$HPCLIB_JUPYTER_CONFIG_DIR" && export JUPYTER_CONFIG_DIR="$HPCLIB_JUPYTER_CONFIG_DIR" ||
    echo "hpclib: could not create $HPCLIB_JUPYTER_CONFIG_DIR; using Jupyter's default config directory" >&2
fi
start_dir="${HPCLIB_JUPYTER_START_DIR:-/}"
if [ ! -d "$start_dir" ]; then
  echo "hpclib: the start directory $start_dir doesn't exist; starting in /" >&2
  start_dir=/
fi

echo "Launching Jupyter on $PROCESS_PORT, starting in $start_dir"
# Run JupyterLab on specified port
jupyter lab --port=$PROCESS_PORT --notebook-dir="$start_dir" --no-browser
