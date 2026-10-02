#!/bin/bash
set -euo pipefail
case "$HPC_PARAM_SCRIPT" in
  *.py) ;;
  *) echo "not a .py file: $HPC_PARAM_SCRIPT" >&2; exit 2 ;;
esac
export OMP_NUM_THREADS="$HPC_PARAM_CPUS"
exec python3 "$HPC_PARAM_SCRIPT"
