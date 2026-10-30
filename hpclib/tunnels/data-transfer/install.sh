#!/usr/bin/env bash
# hpclib-install: --check --force
# Gets the data-transfer-tools image to SMB_IMAGE from SMB_IMAGE_SOURCE: pulled (oras://, docker://, library://,
# shub://), built from a .def file (singularity build --fakeroot), or copied from a .sif. --check reports whether
# it supplies smbclient; rclone is checked separately for the optional GUI.
set -e
set -o pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/settings.sh"
runtime=$(command -v singularity || command -v apptainer || true)

if [ -f "$SMB_IMAGE" ] && [ "${1:-}" != --force ]; then
  if [ -n "$runtime" ] && version=$("$runtime" exec "$SMB_IMAGE" smbclient --version 2>/dev/null); then
    gui='unavailable'
    if rclone=$("$runtime" exec "$SMB_IMAGE" rclone version 2>/dev/null | head -n 1); then gui="$rclone"; fi
    printf 'installed: %s (%s; optional GUI: %s)\n' "$SMB_IMAGE" "$version" "$gui"
    exit 0
  fi
  if [ "${1:-}" = --check ]; then
    printf 'not installed: %s has no smbclient (or %s cannot run it)\n' "$SMB_IMAGE" "${runtime:-singularity}"
    exit 1
  fi
fi
if [ "${1:-}" = --check ]; then
  printf 'not installed: no image at %s\n' "$SMB_IMAGE"
  exit 1
fi
if [ -z "$runtime" ]; then
  echo "neither singularity nor apptainer is on this node's PATH; load its module in ~/.bashrc" >&2
  exit 1
fi
mkdir -p "$(dirname "$SMB_IMAGE")"
partial="$SMB_IMAGE.partial.$$"
trap 'rm -f "$partial"' EXIT
case "$SMB_IMAGE_SOURCE" in
  oras://*|docker://*|library://*|shub://*|https://*) "$runtime" pull "$partial" "$SMB_IMAGE_SOURCE" ;;
  *.def) "$runtime" build --fakeroot "$partial" "$SMB_IMAGE_SOURCE" ;;
  *.sif) cp "$SMB_IMAGE_SOURCE" "$partial" ;;
  *) echo "SMB_IMAGE_SOURCE must be a URI (oras://, docker://, library://), a .def or a .sif" >&2; exit 1 ;;
esac
mv -f "$partial" "$SMB_IMAGE"
printf 'installed: %s\n' "$SMB_IMAGE"
