#!/usr/bin/env bash
# hpclib-install: --check --force
# Installs what the PAI job runs: the proto-auto-interface checkout, PAI_ROOT_DIR/proto-auto-interface (cloned
# from PAI_REPO), the app's image, PAI_CONTAINER (pulled from PAI_IMAGE), and the database's,
# PAI_POSTGRES_CONTAINER (from PAI_POSTGRES_IMAGE). --check only reports whether they are there.
# --force updates: fast-forwards the checkout from its own remote and pulls the app's image again. The
# database's image is left as it is (a newer postgres can refuse the data directory the old one wrote); delete
# PAI_POSTGRES_CONTAINER to have it pulled again. A database job already running keeps what it started with.
set -e
tunnel_source_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$tunnel_source_dir/user.sh"
target="$PAI_ROOT_DIR/proto-auto-interface"
mode="${1:-}"

commit() { git -C "$target" log -1 --format='%h %cs' 2>/dev/null || echo "not a git checkout"; }
built() { date -r "$1" +%F 2>/dev/null || echo "?"; }
summary() {
  printf 'installed: %s (%s); image %s (pulled %s)\n' "$target" "$(commit)" "$PAI_CONTAINER" "$(built "$PAI_CONTAINER")"
}

missing=""
[ -f "$target/singularity-compose.sh" ] || missing="no $target/singularity-compose.sh"
[ -n "$missing" ] || [ -f "$PAI_CONTAINER" ] || missing="no image at $PAI_CONTAINER"
[ -n "$missing" ] || [ -f "$PAI_POSTGRES_CONTAINER" ] || missing="no database image at $PAI_POSTGRES_CONTAINER"
if [ "$mode" = --check ]; then
  if [ -n "$missing" ]; then printf 'not installed: %s\n' "$missing"; exit 1; fi
  summary
  exit 0
fi
if [ -z "$missing" ] && [ "$mode" != --force ]; then
  summary
  exit 0
fi

runtime=$(command -v singularity || command -v apptainer || true)
# pulled beside the target, then moved over it, so a failed pull leaves the old image (or none), never half of one,
# and a running job keeps the file it opened
pull() {
  local dest="$1" source="$2" partial
  if [ -z "$runtime" ]; then
    echo "neither singularity nor apptainer is on this node's PATH; load its module in ~/.bashrc" >&2
    exit 1
  fi
  mkdir -p "$(dirname "$dest")"
  partial="$dest.partial.$$"
  trap 'rm -f "$partial"' EXIT
  echo "pulling $source to $dest"
  "$runtime" pull "$partial" "$source"
  mv -f "$partial" "$dest"
  trap - EXIT
}

if [ ! -f "$target/singularity-compose.sh" ]; then
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
elif [ "$mode" = --force ]; then
  if ! git -C "$target" rev-parse --git-dir > /dev/null 2>&1; then
    printf '%s is not a git checkout, so it is left as it is\n' "$target" >&2
  else
    # only a fast-forward: your own commits or uncommitted changes that conflict stop it, and nothing is lost
    echo "updating $target ($(git -C "$target" rev-parse --abbrev-ref HEAD), $(commit))"
    if ! GIT_TERMINAL_PROMPT=0 git -C "$target" pull --ff-only; then
      printf 'could not fast-forward %s (local commits or changes in the way?); the images are not pulled\n' \
        "$target" >&2
      exit 1
    fi
  fi
fi

if [ "$mode" = --force ] || [ ! -f "$PAI_CONTAINER" ]; then
  pull "$PAI_CONTAINER" "$PAI_IMAGE"
fi
if [ ! -f "$PAI_POSTGRES_CONTAINER" ]; then
  pull "$PAI_POSTGRES_CONTAINER" "$PAI_POSTGRES_IMAGE"
fi
if [ "$mode" = --force ]; then
  echo "a database job already running keeps running what it started with until it is restarted (End database job," \
    "then Start)"
fi
summary
