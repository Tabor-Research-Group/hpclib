#!/usr/bin/env bash
# hpclib-install: --check
# Installs the proto-auto-interface checkout the PAI job runs, PAI_ROOT_DIR/proto-auto-interface, by cloning
# PAI_REPO. --check only reports whether it is there.
set -e
tunnel_source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$tunnel_source_dir/user.sh"
target="$PAI_ROOT_DIR/proto-auto-interface"

if [ -f "$target/singularity-compose.sh" ]; then
  printf 'installed: %s\n' "$target"
  exit 0
fi
if [ "${1:-}" = --check ]; then
  printf 'not installed: no %s\n' "$target/singularity-compose.sh"
  exit 1
fi
if [ -z "${PAI_REPO:-}" ]; then
  printf 'set PAI_REPO (the proto-auto-interface git URL) to install it, or put a checkout at %s\n' "$target" >&2
  exit 1
fi
if [ -e "$target" ]; then
  printf '%s exists but has no singularity-compose.sh; move it aside first\n' "$target" >&2
  exit 1
fi
mkdir -p "$PAI_ROOT_DIR"
GIT_TERMINAL_PROMPT=0 git clone "$PAI_REPO" "$target"
printf 'installed: %s\n' "$target"
