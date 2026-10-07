export PAI_USER="${PAI_USER:-$(whoami)}"
export PAI_ROOT_DIR="${PAI_ROOT_DIR:-/scratch/user/$PAI_USER/pai}"
export INCLUDE_DEV_ENDPOINTS="${INCLUDE_DEV_ENDPOINTS:-true}"
# 1: singularity-compose.sh binds the proto-auto-interface source into the containers (your changes run); 0: off
export PAI_BIND_SOURCE="${PAI_BIND_SOURCE:-1}"
# the images singularity-compose.sh runs (the same defaults as it has), and where install.sh pulls them from
export PAI_CONTAINER="${PAI_CONTAINER:-$PAI_ROOT_DIR/proto-auto-interface.sif}"
export PAI_IMAGE="${PAI_IMAGE:-docker://ghcr.io/tabor-research-group/proto-auto-interface:master}"
export PAI_POSTGRES_CONTAINER="${PAI_POSTGRES_CONTAINER:-$PAI_ROOT_DIR/docker-postgres-rdkit.sif}"
export PAI_POSTGRES_IMAGE="${PAI_POSTGRES_IMAGE:-docker://mcs07/postgres-rdkit:latest}"
