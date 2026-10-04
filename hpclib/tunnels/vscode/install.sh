#!/usr/bin/env bash
# hpclib-install: --check --force
# Pulls the code-server image to VSCODE_CONTAINER (see user.sh), the path the job runs it from.
# --check only reports whether it is there; --force pulls it again (the newest image).
set -e

# Use the same image path as the VS Code sbatch script. Environment
# overrides (for clusters without /scratch/user) are respected.
tunnel_source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$tunnel_source_dir/user.sh"

if [ -f "$VSCODE_CONTAINER" ] && [ "${1:-}" != --force ]; then
  printf 'installed: %s\n' "$VSCODE_CONTAINER"
  exit 0
fi
if [ "${1:-}" = --check ]; then
  printf 'not installed: no image at %s\n' "$VSCODE_CONTAINER"
  exit 1
fi

mkdir -p "$(dirname "$VSCODE_CONTAINER")"
runtime=$(command -v singularity || command -v apptainer || true)
if [ -z "$runtime" ]; then
  echo "neither singularity nor apptainer is on this node's PATH; load its module in ~/.bashrc" >&2
  exit 1
fi
# pulled beside the target, then moved, so a failed pull leaves no half image the job would try to run
partial="$VSCODE_CONTAINER.partial.$$"
trap 'rm -f "$partial"' EXIT
"$runtime" pull "$partial" docker://codercom/code-server:latest
mv -f "$partial" "$VSCODE_CONTAINER"
printf 'installed: %s\n' "$VSCODE_CONTAINER"
