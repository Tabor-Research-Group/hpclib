#!/bin/bash
# Runs on the cluster's login node (tunnel_setup runs it there over ssh; the console's Install and Check use it):
# saves a tunnel's settings, and checks or installs what the tunnel needs with its install.sh.
#
#   setup_tunnel.sh TUNNEL [--set NAME=VALUE]... [--save] [--install [--force]] [--check]
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
sets=() save=false install=false force=false check=false
while [ "$#" -gt 0 ]; do
  case "$1" in
    --set) sets+=("$2"); shift 2 ;;
    --set=*) sets+=("${1#--set=}"); shift ;;
    --save) save=true; shift ;;
    --install) install=true; shift ;;
    --force) force=true; shift ;;
    --check) check=true; shift ;;
    *) echo "$usage" >&2; exit 2 ;;
  esac
done
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

if [ "$check" = true ]; then
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
