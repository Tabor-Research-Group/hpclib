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
# Where the database's nginx listens on its node. 127.0.0.1 (the default) makes it reachable only from that node
# itself and through tunnels, so a job on any other node cannot use it. Set a cluster address, or 0.0.0.0, to let
# jobs on every node connect. The database has no login of its own: anyone who can reach that address can read and
# write it, so use the narrowest address that works. Applies to the next database job.
export PAI_BIND_HOST="${PAI_BIND_HOST:-127.0.0.1}"
# A directory that compute nodes can read, in which the running database announces where it is listening (one small
# JSON file, refreshed while the job runs and removed when it ends) for clients that discover it, such as
# database_discovery. Empty: no announcement.
export PAI_ANNOUNCE_DIR="${PAI_ANNOUNCE_DIR:-}"
