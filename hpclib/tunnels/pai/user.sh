export PAI_USER="${PAI_USER:-$(whoami)}"
export PAI_ROOT_DIR="${PAI_ROOT_DIR:-/scratch/user/$PAI_USER/pai}"
export INCLUDE_DEV_ENDPOINTS="${INCLUDE_DEV_ENDPOINTS:-true}"
# 1: singularity-compose.sh binds the proto-auto-interface source into the containers (your changes run); 0: off
export PAI_BIND_SOURCE="${PAI_BIND_SOURCE:-1}"
