unset ENABLE_WEB_PROXY
unset START_GIT_SERVER
unset START_SLURM_SERVER
DEFAULT_SBATCH_ARGS="--time=6-23:59:59 --mem=10gb --ntasks=1 --cpus-per-task=4 --nodelist=chem-entr-c04"
PROCESS_PORT=3100
SHARED_INSTANCE=true        # connect to the database another job already runs, if one does
PROCESS_PORT_FROM_JOB=true  # the job takes PROCESS_PORT if it is free on its node, else another, and reports it
KEEP_INSTANCE=true          # closing the tunnel leaves the database running for the next one
# settings (setup_tunnel.sh --set, the console's tunnel settings) its install.sh and job read
TUNNEL_SETTINGS="PAI_ROOT_DIR PAI_REPO PAI_IMAGE INCLUDE_DEV_ENDPOINTS PAI_BIND_SOURCE"
