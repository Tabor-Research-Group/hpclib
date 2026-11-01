#!/usr/bin/env bash
# hpclib-install: --check --force
# Pulls the code-server image (tag VSCODE_IMAGE_TAG, see user.sh) to VSCODE_CONTAINER, the path the job runs it from,
# and writes the tag beside it (VSCODE_CONTAINER.tag), so a different tag in the settings counts as not installed yet.
# --check only reports whether it is there; --force pulls it again (the newest image for that tag).
set -e

# Use the same image path as the VS Code sbatch script. Environment
# overrides (for clusters without /scratch/user) are respected.
tunnel_source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$tunnel_source_dir/user.sh"

if ! [[ "$VSCODE_IMAGE_TAG" =~ ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$ ]]; then
  echo "VSCODE_IMAGE_TAG '$VSCODE_IMAGE_TAG' is not an image tag (letters, digits, _ . -)" >&2
  exit 1
fi

# what an image already there was pulled as: its .tag file (an image from before there was one counts as latest)
have_tag=''
[ ! -f "$VSCODE_CONTAINER.tag" ] || have_tag=$(head -n 1 "$VSCODE_CONTAINER.tag")
if [ -f "$VSCODE_CONTAINER" ] && { [ "$have_tag" = "$VSCODE_IMAGE_TAG" ] || { [ -z "$have_tag" ] && [ "$VSCODE_IMAGE_TAG" = latest ]; }; } &&
    [ "${1:-}" != --force ]; then
  printf 'installed: %s (%s)\n' "$VSCODE_CONTAINER" "${have_tag:-latest}"
  exit 0
fi
if [ "${1:-}" = --check ]; then
  if [ -f "$VSCODE_CONTAINER" ]; then
    printf 'not installed: the image at %s is %s, the settings ask for %s: Install pulls it\n' \
      "$VSCODE_CONTAINER" "${have_tag:-unknown}" "$VSCODE_IMAGE_TAG"
  else
    printf 'not installed: no image at %s\n' "$VSCODE_CONTAINER"
  fi
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
"$runtime" pull "$partial" "docker://codercom/code-server:$VSCODE_IMAGE_TAG"
mv -f "$partial" "$VSCODE_CONTAINER"
printf '%s\n' "$VSCODE_IMAGE_TAG" > "$VSCODE_CONTAINER.tag"
printf 'installed: %s (%s)\n' "$VSCODE_CONTAINER" "$VSCODE_IMAGE_TAG"
