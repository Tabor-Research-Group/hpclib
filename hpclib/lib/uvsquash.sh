# uv environments packed into one SquashFS file: one file on the parallel filesystem instead of tens of thousands,
# and the same `uv run` inside it.
#
#   hpclib_uvsquash build PROJECT IMAGE.sqfs   sync PROJECT's locked environment, with its Python, into IMAGE
#   hpclib_uvsquash mount IMAGE.sqfs           mount IMAGE where it was built (squashfuse; no root)
#   hpclib_uvsquash umount IMAGE.sqfs
#   hpclib_uvsquash env IMAGE.sqfs             the exports that make uv in PROJECT use the packed environment
#   hpclib_uvsquash bind IMAGE.sqfs            the Singularity/Apptainer --bind that mounts it inside a container
#
# A uv environment isn't relocatable: .venv/bin/python links to a Python under UV_PYTHON_INSTALL_DIR, and the
# scripts' #! lines and pyvenv.cfg hold absolute paths. So the environment is built at the path the image is later
# mounted at, $HPCLIB_UVSQUASH_ROOT/NAME (NAME: the image's file name without .sqfs), with its Python inside it
# (venv/ and python/), and the image is mounted back there: by squashfuse on a login node, or by Singularity or
# Apptainer (--bind IMAGE:MOUNT:image-src=/), which template jobs already run in. HPCLIB_UVSQUASH_ROOT must be the
# same path on every node: scratch is the usual place.
#
# The image is read-only, so bytecode is compiled at build time, and `uv run` is told not to sync (UV_NO_SYNC,
# UV_FROZEN). Changing dependencies (uv add, uv lock) works as usual in the project; build the image again after.
# The project itself is installed editable, as `uv sync` does: its source stays in PROJECT, outside the image, so
# editing your code needs no rebuild (HPCLIB_UVSQUASH_SYNC_ARGS="--no-editable" packs it in instead).

HPCLIB_UVSQUASH_ROOT="${HPCLIB_UVSQUASH_ROOT:-${SCRATCH:-$HOME/.local/share}/uvsquash}"
HPCLIB_UVSQUASH_COMP="${HPCLIB_UVSQUASH_COMP:-zstd}"   # gzip for squashfs-tools older than 4.4
HPCLIB_UVSQUASH_SYNC_ARGS="${HPCLIB_UVSQUASH_SYNC_ARGS:-}"

_hpclib_uvsquash_mountpoint() {  # _hpclib_uvsquash_mountpoint IMAGE: where IMAGE's environment lives
  local name
  name=$(basename "$1")
  printf '%s/%s' "$HPCLIB_UVSQUASH_ROOT" "${name%.sqfs}"
}

_hpclib_uvsquash_mounted() {  # _hpclib_uvsquash_mounted DIR: whether something is mounted at DIR
  grep -qs " $(printf '%s' "$1" | sed 's/ /\\040/g') " /proc/mounts
}

_hpclib_uvsquash_build() {  # _hpclib_uvsquash_build PROJECT IMAGE
  local project="$1" image="$2" mnt uv status
  [ -f "$project/pyproject.toml" ] || { echo "hpclib_uvsquash build: $project has no pyproject.toml" >&2; return 2; }
  [ -f "$project/uv.lock" ] || { echo "hpclib_uvsquash build: $project has no uv.lock; run uv lock there first" >&2; return 2; }
  case "$image" in *.sqfs) ;; *) echo "hpclib_uvsquash build: IMAGE ends in .sqfs" >&2; return 2 ;; esac
  command -v mksquashfs > /dev/null 2>&1 || { echo "hpclib_uvsquash build: needs mksquashfs (squashfs-tools)" >&2; return 1; }
  uv=$(_hpclib_pytools_find uv) || { echo "hpclib_uvsquash build: no uv; hpclib_pytools install uv" >&2; return 1; }
  mnt=$(_hpclib_uvsquash_mountpoint "$image")
  _hpclib_uvsquash_mounted "$mnt" && { echo "hpclib_uvsquash build: $mnt is mounted; hpclib_uvsquash umount $image" >&2; return 1; }
  if [ -n "$(ls -A "$mnt" 2> /dev/null)" ]; then
    echo "hpclib_uvsquash build: $mnt isn't empty (a build that stopped?); remove it first" >&2
    return 1
  fi
  mkdir -p "$mnt" || return 1
  hpclib_pytools_env
  # the Python too goes into the image (only-managed: never a module's Python, which a job might not see)
  # shellcheck disable=SC2086
  (cd "$project" && env UV_PROJECT_ENVIRONMENT="$mnt/venv" UV_PYTHON_INSTALL_DIR="$mnt/python" \
      UV_PYTHON_PREFERENCE=only-managed UV_LINK_MODE=copy \
      "$uv" sync --frozen --compile-bytecode $HPCLIB_UVSQUASH_SYNC_ARGS)
  status=$?
  if [ "$status" = 0 ]; then
    mksquashfs "$mnt" "$image.tmp" -noappend -comp "$HPCLIB_UVSQUASH_COMP" -no-xattrs -quiet -no-progress \
      && mv -f "$image.tmp" "$image"
    status=$?
    rm -f "$image.tmp"
  fi
  # the files are in the image now; the empty directory stays as the mount point
  find "$mnt" -mindepth 1 -delete
  return "$status"
}

_hpclib_uvsquash_mount() {  # _hpclib_uvsquash_mount IMAGE
  local mnt
  [ -f "$1" ] || { echo "hpclib_uvsquash mount: no image $1" >&2; return 2; }
  command -v squashfuse > /dev/null 2>&1 \
    || { echo "hpclib_uvsquash mount: needs squashfuse (or use the image in a container: hpclib_uvsquash bind)" >&2; return 1; }
  mnt=$(_hpclib_uvsquash_mountpoint "$1")
  _hpclib_uvsquash_mounted "$mnt" && return 0
  mkdir -p "$mnt" && squashfuse "$1" "$mnt"
}

_hpclib_uvsquash_umount() {  # _hpclib_uvsquash_umount IMAGE
  local mnt
  mnt=$(_hpclib_uvsquash_mountpoint "$1")
  _hpclib_uvsquash_mounted "$mnt" || return 0
  fusermount3 -u "$mnt" 2> /dev/null || fusermount -u "$mnt"
}

_hpclib_uvsquash_env() {  # _hpclib_uvsquash_env IMAGE: export lines, for eval
  local mnt
  mnt=$(_hpclib_uvsquash_mountpoint "$1")
  printf 'export UV_PROJECT_ENVIRONMENT=%q UV_PYTHON_INSTALL_DIR=%q UV_NO_SYNC=1 UV_FROZEN=1 UV_PYTHON_DOWNLOADS=never\n' \
    "$mnt/venv" "$mnt/python"
}

hpclib_uvsquash() {
  local cmd="${1:-}"
  [ "$#" -gt 0 ] && shift
  case "$cmd" in
    build) [ "$#" = 2 ] || { echo "hpclib_uvsquash build PROJECT IMAGE.sqfs" >&2; return 2; }; _hpclib_uvsquash_build "$@" ;;
    mount) [ "$#" = 1 ] || { echo "hpclib_uvsquash mount IMAGE.sqfs" >&2; return 2; }; _hpclib_uvsquash_mount "$1" ;;
    umount) [ "$#" = 1 ] || { echo "hpclib_uvsquash umount IMAGE.sqfs" >&2; return 2; }; _hpclib_uvsquash_umount "$1" ;;
    env) [ "$#" = 1 ] || { echo "hpclib_uvsquash env IMAGE.sqfs" >&2; return 2; }; _hpclib_uvsquash_env "$1" ;;
    bind)
      [ "$#" = 1 ] || { echo "hpclib_uvsquash bind IMAGE.sqfs" >&2; return 2; }
      printf '%s:%s:image-src=/\n' "$(cd "$(dirname "$1")" && pwd)/$(basename "$1")" "$(_hpclib_uvsquash_mountpoint "$1")"
      ;;
    *) echo "hpclib_uvsquash build|mount|umount|env|bind ..." >&2; return 2 ;;
  esac
}
