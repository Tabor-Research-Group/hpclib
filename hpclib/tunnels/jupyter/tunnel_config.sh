DEFAULT_PORT=8950
PROCESS_PORT=8888
DEFAULT_SBATCH_ARGS="--time=0-8:00:00 --mem=30gb --ntasks-per-node=4 --nodes=1"
# settings (setup_tunnel.sh --set, the console's tunnel settings) its install.sh and job read
TUNNEL_SETTINGS="CONDA_ENVIRONMENT HPCLIB_JUPYTER_MODULES HPCLIB_JUPYTER_PROJECT"
