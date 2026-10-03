#!/bin/bash
set -euo pipefail
case "$HPC_PARAM_SCRIPT" in
  *.py) ;;
  *) echo "not a .py file: $HPC_PARAM_SCRIPT" >&2; exit 2 ;;
esac
export OMP_NUM_THREADS="$HPC_PARAM_CPUS" MKL_NUM_THREADS="$HPC_PARAM_CPUS" OPENBLAS_NUM_THREADS="$HPC_PARAM_CPUS"
echo "python: $(command -v python) ($(python --version 2>&1))"
read -r -a args <<< "$HPC_PARAM_ARGS"
exec python "$HPC_PARAM_SCRIPT" "${args[@]}"
