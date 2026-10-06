#!/bin/bash
# Runs on the cluster's login node (tunnel_setup runs it there over ssh; the console's Install and Check use it):
# saves a tunnel's settings, and checks or installs what the tunnel needs with its install.sh.
#
#   setup_tunnel.sh TUNNEL [--receive] [--set NAME=VALUE]... [--save] [--install [--force]] [--check]
#                   [--instances] [--stop-instance JOB]
#
#   --secrets         read secrets for the tunnel from standard input, one NAME=BASE64 a line (an empty value
#                     removes one): each must be in the tunnel's TUNNEL_SECRETS (tunnel_config.sh). Each is kept
#                     in $HPCTUNNELS_DATA_DIR/secrets/TUNNEL/NAME (mode 600; never in settings, logs or
#                     arguments), for the tunnel's scripts to read. Ends with HPCLIB_TUNNEL_SECRETS NAME:set|unset...
#                     (also printed by --check). The console's settings send them; not with --receive.
#   --receive         first read a tar archive from standard input (tunnel_setup --push, which the console sends
#                     for packaged apps and settings): its tunnel/ directory, if any, becomes the tunnel, installed
#                     in $HPCLIB_TUNNEL_INSTALL_LOCATION/TUNNEL (never one of hpclib's own tunnels), and its
#                     settings.d/ replaces $HPCTUNNELS_DATA_DIR/settings/TUNNEL.d, files the tunnel's scripts may
#                     read (e.g. data-transfer.d/rclone.conf, more remotes for the rclone web GUI)
#
#   --set NAME=VALUE  a setting: one of the names in the tunnel's TUNNEL_SETTINGS (tunnel_config.sh), e.g.
#                     VSCODE_CONTAINER=/scratch/user/me/images/vscode.sif. With --save, all of them replace the
#                     tunnel's settings file, $HPCTUNNELS_DATA_DIR/settings/TUNNEL.sh, which start_tunnel.sh
#                     reads (so the job and install.sh see the same paths); without it they apply to this run.
#   --install         run the tunnel's install.sh (--force: its --force, e.g. pull an image again)
#   --check           report whether it is installed, on a last line
#                       HPCLIB_TUNNEL_STATUS installed|missing|unknown|nothing MESSAGE
#                     (unknown: its install.sh can't check; nothing: it has nothing to install)
#
#   --instances       the tunnel's registered running instances (see instances.sh), one line each:
#                       HPCLIB_TUNNEL_INSTANCE JOB NODE PORT STATE OWNER
#   --stop-instance JOB
#                     end the job serving an instance (a shared database, say): only a job registered for this
#                     tunnel, and only if you own it. Ends with HPCLIB_TUNNEL_INSTANCE_STOPPED JOB, or
#                     HPCLIB_TUNNEL_INSTANCE_GONE JOB if it had already ended.
#
# An install.sh that can check says so on a line "# hpclib-install: --check" (and "--force" if it takes it);
# one that doesn't is never run for a check, since it would install instead.

set -a
source ~/.bashrc > /dev/null 2>&1 || true
if [ -z "$HPCLIB_DIR" ]; then
  HPCLIB_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
source "$HPCLIB_DIR/hpclib.sh" > /dev/null
HPCTUNNELS_DATA_DIR="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}"

usage='usage: setup_tunnel.sh TUNNEL [--set NAME=VALUE]... [--save] [--install [--force]] [--check]'
TUNNEL_NAME="$1"
shift || true
sets=() save=false install=false force=false check=false instances=false stop_job='' receive=false secrets=false
source "$HPCLIB_DIR/tunnels/instances.sh"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --set) sets+=("$2"); shift 2 ;;
    --set=*) sets+=("${1#--set=}"); shift ;;
    --save) save=true; shift ;;
    --install) install=true; shift ;;
    --force) force=true; shift ;;
    --check) check=true; shift ;;
    --instances) instances=true; shift ;;
    --receive) receive=true; shift ;;
    --secrets) secrets=true; shift ;;
    --stop-instance) stop_job="$2"; shift 2 ;;
    --stop-instance=*) stop_job="${1#--stop-instance=}"; shift ;;
    *) echo "$usage" >&2; exit 2 ;;
  esac
done
if [ "$receive" = true ]; then
  if ! _hpclib_valid_tunnel_name "$TUNNEL_NAME"; then
    echo "setup_tunnel: invalid tunnel name '$TUNNEL_NAME'" >&2
    exit 2
  fi
  incoming=$(mktemp -d "${TMPDIR:-/tmp}/hpclib-receive.XXXXXX") || exit 1
  trap 'rm -rf "$incoming"' EXIT
  # plain files and directories only: no links, nothing outside the archive's own directory
  if ! tar -x -C "$incoming" --no-same-owner --no-same-permissions -f - ||
      [ -n "$(find "$incoming" ! -type f ! -type d -print -quit)" ]; then
    echo "setup_tunnel: the files sent for $TUNNEL_NAME are not a plain tar archive" >&2
    exit 1
  fi
  if [ -d "$incoming/tunnel" ]; then
    if [ -d "$HPCLIB_DIR/tunnels/$TUNNEL_NAME" ]; then
      echo "setup_tunnel: $TUNNEL_NAME is one of hpclib's own tunnels; a package can't replace it" >&2
      exit 1
    fi
    target="$HPCLIB_TUNNEL_INSTALL_LOCATION/$TUNNEL_NAME"
    mkdir -p "$HPCLIB_TUNNEL_INSTALL_LOCATION" && rm -rf "$target.new" && mv "$incoming/tunnel" "$target.new" &&
      chmod -R go-w "$target.new" && rm -rf "$target.old" && { [ ! -e "$target" ] || mv "$target" "$target.old"; } &&
      mv "$target.new" "$target" && rm -rf "$target.old" || { echo "setup_tunnel: could not install $target" >&2; exit 1; }
    echo "received the $TUNNEL_NAME tunnel into $target"
  fi
  settings_d="$HPCTUNNELS_DATA_DIR/settings/$TUNNEL_NAME.d"
  if [ -d "$incoming/settings.d" ]; then
    mkdir -p "$HPCTUNNELS_DATA_DIR/settings" && rm -rf "$settings_d.new" && mv "$incoming/settings.d" "$settings_d.new" &&
      chmod -R go-rwx "$settings_d.new" && rm -rf "$settings_d" && mv "$settings_d.new" "$settings_d" ||
      { echo "setup_tunnel: could not write $settings_d" >&2; exit 1; }
    count=$(find "$settings_d" -type f | wc -l | tr -d ' ')
    [ "$count" = 0 ] && rm -rf "$settings_d" || echo "received $count settings file(s) for $TUNNEL_NAME into $settings_d"
  fi
fi
if ! TUNNEL_DIR=$(resolve_tunnel "$TUNNEL_NAME"); then
  echo "Tunnel '$TUNNEL_NAME' not found in HPCLIB_TUNNEL_PATH ($HPCLIB_TUNNEL_PATH)" >&2
  exit 1
fi

TUNNEL_SETTINGS=""
if tunnel_config_path=$(resolve_tunnel_file tunnel_config.sh); then
  source "$tunnel_config_path"
fi
settings_file="$HPCTUNNELS_DATA_DIR/settings/$TUNNEL_NAME.sh"

lines=()
for kv in "${sets[@]}"; do
  name="${kv%%=*}"
  value="${kv#*=}"
  if [ "$name" = "$kv" ] || ! [[ "$name" =~ ^[A-Z_][A-Z0-9_]*$ ]] || ! [[ " $TUNNEL_SETTINGS " == *" $name "* ]]; then
    echo "setup_tunnel: $TUNNEL_NAME has no setting '$name' (its settings: ${TUNNEL_SETTINGS:-none})" >&2
    exit 2
  fi
  case "$value" in *$'\n'*|*$'\r'*) echo "setup_tunnel: $name must be one line" >&2; exit 2 ;; esac
  lines+=("$(printf 'export %s=%q' "$name" "$value")")
done
if [ "$save" = true ]; then
  mkdir -p "${settings_file%/*}"
  {
    echo "# $TUNNEL_NAME tunnel settings, written by setup_tunnel.sh (the console's tunnel settings); start_tunnel.sh reads them"
    printf '%s\n' "${lines[@]}"
  } > "$settings_file.tmp" && mv -f "$settings_file.tmp" "$settings_file"
  echo "saved ${#lines[@]} setting(s) for $TUNNEL_NAME in $settings_file"
fi
if [ -f "$settings_file" ]; then
  source "$settings_file"
fi
for line in "${lines[@]}"; do eval "$line"; done

installer="$TUNNEL_DIR/install.sh"
can() { [ -f "$installer" ] && grep -q "^# hpclib-install:.*$1" "$installer"; }
status() { printf 'HPCLIB_TUNNEL_STATUS %s %s\n' "$1" "$2"; }

if [ "$install" = true ]; then
  if [ ! -f "$installer" ]; then
    echo "$TUNNEL_NAME has nothing to install"
  else
    args=()
    if [ "$force" = true ]; then
      if can --force; then args+=(--force); else echo "($TUNNEL_NAME's install.sh has no --force; running it as is)"; fi
    fi
    echo "running $installer ${args[*]}"
    if ! (cd "$TUNNEL_DIR" && HPCLIB_TUNNEL_DIR="$TUNNEL_DIR" bash ./install.sh "${args[@]}"); then
      echo "setup_tunnel: $TUNNEL_NAME's install.sh failed" >&2
      [ "$check" = true ] && status missing "its install.sh failed"
      exit 1
    fi
  fi
fi

# The tunnel's secrets (TUNNEL_SECRETS), as files only you can read
secrets_dir="$HPCTUNNELS_DATA_DIR/secrets/$TUNNEL_NAME"
secrets_status() {
  local name out=''
  for name in $TUNNEL_SECRETS; do
    if [ -s "$secrets_dir/$name" ]; then out="$out $name:set"; else out="$out $name:unset"; fi
  done
  printf 'HPCLIB_TUNNEL_SECRETS%s\n' "$out"
}
if [ "$secrets" = true ]; then
  if [ "$receive" = true ]; then
    echo "setup_tunnel: --secrets and --receive both read standard input; send them separately" >&2
    exit 2
  fi
  if [ -z "${TUNNEL_SECRETS:-}" ]; then
    echo "setup_tunnel: $TUNNEL_NAME has no secrets (TUNNEL_SECRETS in its tunnel_config.sh)" >&2
    exit 2
  fi
  (umask 077 && mkdir -p "$secrets_dir") || exit 1
  chmod 700 "$HPCTUNNELS_DATA_DIR/secrets" "$secrets_dir"
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    name="${line%%=*}" value="${line#*=}"
    if [ "$name" = "$line" ] || ! [[ "$name" =~ ^[A-Z_][A-Z0-9_]*$ ]] || [[ " $TUNNEL_SECRETS " != *" $name "* ]]; then
      echo "setup_tunnel: $TUNNEL_NAME has no secret '$name' (its secrets: $TUNNEL_SECRETS)" >&2
      exit 2
    fi
    if [ -z "$value" ]; then
      rm -f "$secrets_dir/$name"
      continue
    fi
    # base64 on the way, so any value arrives as it was sent; written beside the old one, then moved over it
    if ! (umask 077 && printf '%s' "$value" | base64 -d > "$secrets_dir/.$name.new" 2> /dev/null) ||
        [ ! -s "$secrets_dir/.$name.new" ]; then
      rm -f "$secrets_dir/.$name.new"
      echo "setup_tunnel: the value sent for $name is not base64" >&2
      exit 2
    fi
    mv -f "$secrets_dir/.$name.new" "$secrets_dir/$name"
  done
  secrets_status
fi

if [ "$check" = true ]; then
  [ -n "${TUNNEL_SECRETS:-}" ] && secrets_status
  if [ ! -f "$installer" ]; then
    status nothing "nothing to install"
  elif ! can --check; then
    status unknown "its install.sh can't check; Install runs it"
  elif out=$(cd "$TUNNEL_DIR" && HPCLIB_TUNNEL_DIR="$TUNNEL_DIR" bash ./install.sh --check 2>&1); then
    status installed "$(printf '%s\n' "$out" | tail -n 1)"
  else
    status missing "$(printf '%s\n' "$out" | tail -n 1)"
  fi
fi

if [ "$instances" = true ]; then
  dir=$(_tunnel_instances_dir "$TUNNEL_NAME")
  for file in $(ls -t "$dir" 2>/dev/null); do
    case "$file" in ''|*[!0-9]*) continue ;; esac
    line=$(squeue -h -j "$file" -o "%T %u" 2>/dev/null | head -n 1)
    if [ -z "$line" ]; then rm -f "$dir/$file"; continue; fi
    read -r node port < "$dir/$file"
    printf 'HPCLIB_TUNNEL_INSTANCE %s %s %s %s\n' "$file" "$node" "$port" "$line"
  done
fi

if [ -n "$stop_job" ]; then
  case "$stop_job" in *[!0-9]*|'') echo "setup_tunnel: --stop-instance takes a job id" >&2; exit 2 ;; esac
  dir=$(_tunnel_instances_dir "$TUNNEL_NAME")
  if [ ! -f "$dir/$stop_job" ]; then
    echo "setup_tunnel: job $stop_job is not a registered $TUNNEL_NAME instance; not cancelling it" >&2
    exit 1
  fi
  owner=$(squeue -h -j "$stop_job" -o %u 2>/dev/null | head -n 1)
  if [ -z "$owner" ]; then
    rm -f "$dir/$stop_job"
    echo "HPCLIB_TUNNEL_INSTANCE_GONE $stop_job"
    exit 0
  fi
  if [ "$owner" != "$(id -un)" ]; then
    echo "setup_tunnel: job $stop_job belongs to $owner, not you; not cancelling it" >&2
    exit 1
  fi
  scancel "$stop_job" || exit 1
  rm -f "$dir/$stop_job"
  echo "HPCLIB_TUNNEL_INSTANCE_STOPPED $stop_job"
fi
