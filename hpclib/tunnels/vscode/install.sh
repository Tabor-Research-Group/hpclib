#!/usr/bin/env bash
set -e

# Use the same image path as the VS Code sbatch script. Environment
# overrides (for clusters without /scratch/user) are respected.
tunnel_source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$tunnel_source_dir/user.sh"

if [ -f "$VSCODE_CONTAINER" ]; then
  printf 'VS Code image already exists: %s\n' "$VSCODE_CONTAINER"
  exit 0
fi

mkdir -p "$(dirname "$VSCODE_CONTAINER")"
singularity pull "$VSCODE_CONTAINER" docker://codercom/code-server:latest
