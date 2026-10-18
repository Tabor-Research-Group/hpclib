#!/bin/bash
# Runs on the cluster's login node (tunnel_setup runs it there over ssh; the console's Install and Check use it):
# saves a tunnel's settings, and checks or installs what the tunnel needs with its install.sh.
#
#   setup_tunnel.sh TUNNEL [--receive] [--set NAME=VALUE]... [--save] [--install [--force]] [--check]
#                   [--instances] [--stop-instance JOB] [--tokens ACTION [ARGS...]] [--downloads ACTION [ARGS...]]
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
#                     HPCLIB_INSTALL_MODULES is accepted for every tunnel: an ordered colon-separated list of
#                     modules to load before install.sh (including --check), e.g. WebProxy:GCC/12.2.0.
#                     An empty value disables preloading. Saved modules also apply to REST installation.
#   --install         run the tunnel's install.sh (--force: its --force, e.g. pull an image again)
#                     exit 3 means it cannot run on this node: HPCLIB_INSTALL_ELSEWHERE asks the console to
#                     continue on the REST node through POST /admin/tunnel-install (owner only).
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
#   --tokens ACTION [ARGS...]
#                     the app's own bearer tokens: runs the tunnel's tokens.sh as `tokens.sh --json ACTION ARGS`
#                     (list; create --label L --scopes a,b --days N [--save-to FILE]; revoke ID), with the
#                     tunnel's settings in its environment, and passes on the one line of JSON it prints, as
#                       HPCLIB_TOKENS {...}          (list)
#                       HPCLIB_TOKEN_CREATED {...}   (create: the token itself, or the file it went to)
#                       HPCLIB_TOKEN_REVOKED ID      (revoke)
#                     It must be the last option. The token is on standard output only, never in a log or argument.
#                     A tokens.sh that can't work on this node (no Singularity or Apptainer on a login node, say)
#                     exits 3, which is passed on as HPCLIB_TOKENS_ELSEWHERE REASON and exit 3: the console then runs
#                     it on the REST server's node instead (POST /admin/tunnel-tokens), through the agent tunnel.
#
#   --downloads ACTION [ARGS...]
#                     what the app downloads to the cluster (OpenCode's models), with the tunnel's downloads.sh:
#                       list                         HPCLIB_DOWNLOADS {...}       (downloads.sh --json list)
#                       pull KIND NAME               downloads.sh pull KIND NAME, its progress as it comes
#                       remove KIND NAME             HPCLIB_DOWNLOAD_REMOVED {...} (downloads.sh --json remove ...)
#                     It must be the last option; a --check given before it runs after a pull.
#
# An install.sh that can check says so on a line "# hpclib-install: --check" (and "--force" if it takes it);
# one that doesn't is never run for a check, since it would install instead.

set -a
source ~/.bashrc > /dev/null 2>&1 || true
if [ -z "$HPCLIB_DIR" ]; then
  HPCLIB_DIR="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
fi
source "$HPCLIB_DIR/hpclib.sh" > /dev/null
# Is a current hpclib loaded? ~/.bashrc may set HPCLIB_DIR to another copy, which is used as always; this only says
# so when that copy is older than the one this script belongs to (its servers and tunnels may lack what this has).
_hpclib_here="$(cd -P "$(dirname "${BASH_SOURCE[0]}")/.." > /dev/null 2>&1 && pwd)"
_hpclib_loaded="$(cd -P "$HPCLIB_DIR" > /dev/null 2>&1 && pwd)"
if [ -n "$_hpclib_here" ] && [ "$_hpclib_loaded" != "$_hpclib_here" ] && [ -f "$_hpclib_here/hpclib.sh" ]; then
  _hpclib_here_version=$(sed -n 's/^HPCLIB_VERSION="\(.*\)"/\1/p' "$_hpclib_here/hpclib.sh")
  if [ "$(_hpclib_version_compare "${HPCLIB_VERSION:-0}" "$_hpclib_here_version" 2> /dev/null)" = -1 ]; then
    echo "hpclib: WARNING: running hpclib ${HPCLIB_VERSION:-(unknown version)} from $HPCLIB_DIR (HPCLIB_DIR, set in" \
      "~/.bashrc), which is older than $_hpclib_here_version in $_hpclib_here, where this started; update that" \
      "copy, or point HPCLIB_DIR at $_hpclib_here" >&2
  fi
fi
unset _hpclib_here _hpclib_loaded _hpclib_here_version
HPCTUNNELS_DATA_DIR="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}"

usage='usage: setup_tunnel.sh TUNNEL [--set NAME=VALUE]... [--save] [--install [--force]] [--check]'
TUNNEL_NAME="$1"
shift || true
sets=() save=false install=false force=false check=false instances=false stop_job='' receive=false secrets=false
tokens=false tokens_action='' tokens_args=()
downloads=false downloads_action='' downloads_args=()
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
    --tokens) tokens=true; tokens_action="${2:-}"; shift; [ "$#" -gt 0 ] && shift; tokens_args=("$@"); break ;;
    --downloads) downloads=true; downloads_action="${2:-}"; shift; [ "$#" -gt 0 ] && shift; downloads_args=("$@"); break ;;
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
TUNNEL_SETTINGS="$TUNNEL_SETTINGS HPCLIB_INSTALL_MODULES"
settings_file="$HPCTUNNELS_DATA_DIR/settings/$TUNNEL_NAME.sh"

valid_install_modules() {
  [ -z "$1" ] || { [ "${#1}" -le 1024 ] && [[ "$1" =~ ^[A-Za-z0-9_.+][A-Za-z0-9_.+/-]{0,127}(:[A-Za-z0-9_.+][A-Za-z0-9_.+/-]{0,127})*$ ]] &&
    [ "$(printf '%s' "$1" | tr ':' '\n' | wc -l)" -lt 32 ]; }
}
lines=()
for kv in "${sets[@]}"; do
  name="${kv%%=*}"
  value="${kv#*=}"
  if [ "$name" = "$kv" ] || ! [[ "$name" =~ ^[A-Z_][A-Z0-9_]*$ ]] || ! [[ " $TUNNEL_SETTINGS " == *" $name "* ]]; then
    echo "setup_tunnel: $TUNNEL_NAME has no setting '$name' (its settings: ${TUNNEL_SETTINGS:-none})" >&2
    exit 2
  fi
  case "$value" in *$'\n'*|*$'\r'*) echo "setup_tunnel: $name must be one line" >&2; exit 2 ;; esac
  if [ "$name" = HPCLIB_INSTALL_MODULES ] && ! valid_install_modules "$value"; then
    echo "setup_tunnel: HPCLIB_INSTALL_MODULES must be up to 32 colon-separated module names (at most 1024 characters)" >&2
    exit 2
  fi
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

run_installer() (
  cd "$TUNNEL_DIR" || exit 1
  # Use the same module initialization paths as the jobs tunnel, including in a noninteractive REST child.
  if [ -n "${HPCLIB_INSTALL_MODULES:-}" ]; then
    if ! valid_install_modules "$HPCLIB_INSTALL_MODULES"; then
      echo "setup_tunnel: invalid HPCLIB_INSTALL_MODULES; use colon-separated module names" >&2
      exit 2
    fi
    if ! type module > /dev/null 2>&1; then
      for module_init in /etc/profile.d/lmod.sh /etc/profile.d/modules.sh /usr/share/lmod/lmod/init/bash; do
        if [ -f "$module_init" ]; then source "$module_init"; break; fi
      done
    fi
    if ! type module > /dev/null 2>&1; then
      echo "setup_tunnel: the module command is unavailable on $(hostname -s); cannot preload installation modules" >&2
      exit 3
    fi
    IFS=: read -r -a install_modules <<< "$HPCLIB_INSTALL_MODULES"
    for install_module in "${install_modules[@]}"; do
      echo "loading installation module $install_module" >&2
      if ! module load "$install_module"; then
        echo "setup_tunnel: could not load installation module $install_module on $(hostname -s)" >&2
        exit 3
      fi
    done
  fi
  HPCLIB_TUNNEL_DIR="$TUNNEL_DIR" bash ./install.sh "$@"
)

if [ "$install" = true ]; then
  if [ ! -f "$installer" ]; then
    echo "$TUNNEL_NAME has nothing to install"
  else
    args=()
    if [ "$force" = true ]; then
      if can --force; then args+=(--force); else echo "($TUNNEL_NAME's install.sh has no --force; running it as is)"; fi
    fi
    echo "running $installer ${args[*]}"
    installer_status=0
    run_installer "${args[@]}" || installer_status=$?
    if [ "$installer_status" = 3 ]; then
      printf 'HPCLIB_INSTALL_ELSEWHERE %s\n' "install.sh cannot run on $(hostname -s); continue on the REST server's node"
      exit 3
    elif [ "$installer_status" != 0 ]; then
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

# The app's own tokens: its tokens.sh does the work (it knows the app's token store); this passes on its JSON
if [ "$tokens" = true ]; then
  case "$tokens_action" in list|create|revoke) ;; *) echo "setup_tunnel: --tokens takes list, create or revoke" >&2; exit 2 ;; esac
  if [ ! -f "$TUNNEL_DIR/tokens.sh" ]; then
    echo "setup_tunnel: $TUNNEL_NAME has no tokens.sh on this cluster (install or update the package with the app's Install)" >&2
    exit 1
  fi
  tokens_err=$(mktemp "${TMPDIR:-/tmp}/hpclib-tokens.XXXXXX") || exit 1
  trap 'rm -f "$tokens_err"' EXIT
  tokens_out=$(cd "$TUNNEL_DIR" && HPCLIB_TUNNEL_DIR="$TUNNEL_DIR" bash ./tokens.sh --json "$tokens_action" "${tokens_args[@]}" 2> "$tokens_err") || {
    tokens_status=$?
    # tokens.sh's own complaint (its standard error never holds a token), so the console can say why
    reason=$(grep -v '^[[:space:]]*$' "$tokens_err" | tail -n 1 | tr -d '\r' | cut -c1-300)
    if [ "$tokens_status" = 3 ]; then     # not on this node: where the REST server runs, the console tries there
      [ -n "$reason" ] || reason="tokens.sh cannot run on $(hostname -s)"
      printf 'HPCLIB_TOKENS_ELSEWHERE %s\n' "$reason"
      exit 3
    fi
    echo "setup_tunnel: tokens.sh $tokens_action failed${reason:+: $reason}" >&2
    exit 1
  }
  tokens_json=$(printf '%s\n' "$tokens_out" | grep '^{' | tail -n 1)
  if [ -z "$tokens_json" ]; then
    echo "setup_tunnel: tokens.sh $tokens_action printed no JSON" >&2
    exit 1
  fi
  case "$tokens_action" in
    list) printf 'HPCLIB_TOKENS %s\n' "$tokens_json" ;;
    create) printf 'HPCLIB_TOKEN_CREATED %s\n' "$tokens_json" ;;
    revoke) printf 'HPCLIB_TOKEN_REVOKED %s\n' "${tokens_args[0]}" ;;
  esac
  exit 0
fi

# What the app downloads (its downloads.sh knows where and how): a list or a removal passes its JSON on; a pull
# shows its progress as it comes, and is followed by --check if that was asked for
if [ "$downloads" = true ]; then
  case "$downloads_action" in
    list) [ "${#downloads_args[@]}" = 0 ] ;;
    pull|remove) [ "${#downloads_args[@]}" = 2 ] ;;
    *) false ;;
  esac || { echo "setup_tunnel: --downloads takes list, or pull or remove with a KIND and a NAME" >&2; exit 2; }
  if [ ! -f "$TUNNEL_DIR/downloads.sh" ]; then
    echo "setup_tunnel: $TUNNEL_NAME has no downloads.sh on this cluster (install or update the package with the app's Install)" >&2
    exit 1
  fi
  if [ "$downloads_action" = pull ]; then
    echo "downloading ${downloads_args[0]} ${downloads_args[1]} on $(hostname -s)"
    (cd "$TUNNEL_DIR" && HPCLIB_TUNNEL_DIR="$TUNNEL_DIR" bash ./downloads.sh pull "${downloads_args[@]}") || {
      echo "setup_tunnel: downloading ${downloads_args[1]} failed (above)" >&2
      exit 1
    }
    [ "$check" = true ] || exit 0
  else
    downloads_err=$(mktemp "${TMPDIR:-/tmp}/hpclib-downloads.XXXXXX") || exit 1
    trap 'rm -f "$downloads_err"' EXIT
    downloads_out=$(cd "$TUNNEL_DIR" && HPCLIB_TUNNEL_DIR="$TUNNEL_DIR" \
      bash ./downloads.sh --json "$downloads_action" "${downloads_args[@]}" 2> "$downloads_err") || {
      reason=$(grep -v '^[[:space:]]*$' "$downloads_err" | tail -n 1 | tr -d '\r' | cut -c1-300)
      echo "setup_tunnel: downloads.sh $downloads_action failed${reason:+: $reason}" >&2
      exit 1
    }
    downloads_json=$(printf '%s\n' "$downloads_out" | grep '^{' | tail -n 1)
    if [ -z "$downloads_json" ]; then
      echo "setup_tunnel: downloads.sh $downloads_action printed no JSON" >&2
      exit 1
    fi
    case "$downloads_action" in
      list) printf 'HPCLIB_DOWNLOADS %s\n' "$downloads_json" ;;
      remove) printf 'HPCLIB_DOWNLOAD_REMOVED %s\n' "$downloads_json" ;;
    esac
    exit 0
  fi
fi

if [ "$check" = true ]; then
  [ -n "${TUNNEL_SECRETS:-}" ] && secrets_status
  if [ ! -f "$installer" ]; then
    status nothing "nothing to install"
  elif ! can --check; then
    status unknown "its install.sh can't check; Install runs it"
  elif out=$(run_installer --check 2>&1); then
    status installed "$(printf '%s\n' "$out" | tail -n 1)"
  else
    check_status=$?
    if [ "$check_status" = 3 ]; then
      status unknown "$(printf '%s\n' "$out" | tail -n 1)"
    else
      status missing "$(printf '%s\n' "$out" | tail -n 1)"
    fi
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
