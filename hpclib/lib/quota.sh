# Quotas: your storage quota and your project's (allocation's) balance, with whatever the cluster has for them.
#
#   hpclib_quota [storage|project|all] [--json]     run the checks here (on a cluster)
#   hpclib_quota set storage|project COMMAND        remember a check for this cluster (COMMAND: a shell command;
#                                                   `default` forgets it; `none` turns that check off)
#   cluster_quota [storage|project|all] [--json] [ssh options] [user@]host    the same, from your machine
#
# Which command each check runs, first found wins:
#   1. $HPCLIB_STORAGE_QUOTA_CHECK / $HPCLIB_PROJECT_QUOTA_CHECK, when set (cluster_quota passes on yours)
#   2. what `hpclib_quota set` (or the console's cluster Settings) saved: $HPCTUNNELS_DATA_DIR/settings/quota.sh
#   3. the default: storage, the first of showquota, myquota and `quota -s` the cluster has; project, myproject
#      (TAMU HPRC's) when the cluster has it
# --json prints one line, HPCLIB_QUOTA {...}: for each check its command, where that came from (env, settings,
# default or none), the exit code, and up to 200 lines of output. The output is the site's own text: hpclib
# doesn't parse it. The REST server's GET /quota, the console and the agents' check_quota run this file.
#
# This file runs on its own too (bash quota.sh [storage|project|all] [--json]), as the REST server runs it.

_hpclib_quota_settings_file() {
  printf '%s/settings/quota.sh' "${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}"
}

_hpclib_quota_command() {  # _hpclib_quota_command storage|project: prints "SOURCE<TAB>COMMAND"
  local kind="$1" var value file c
  case "$kind" in
    storage) var=HPCLIB_STORAGE_QUOTA_CHECK ;;
    project) var=HPCLIB_PROJECT_QUOTA_CHECK ;;
    *) return 2 ;;
  esac
  value="${!var:-}"
  if [ -n "$value" ]; then
    printf 'env\t%s\n' "$value"
    return 0
  fi
  file=$(_hpclib_quota_settings_file)
  if [ -f "$file" ]; then
    value=$(unset "$var"; . "$file" > /dev/null 2>&1; printf '%s' "${!var:-}")
    if [ -n "$value" ]; then
      printf 'settings\t%s\n' "$value"
      return 0
    fi
  fi
  if [ "$kind" = storage ]; then
    for c in showquota myquota; do
      if command -v "$c" > /dev/null 2>&1; then printf 'default\t%s\n' "$c"; return 0; fi
    done
    if command -v quota > /dev/null 2>&1; then printf 'default\t%s\n' "quota -s"; return 0; fi
  elif command -v myproject > /dev/null 2>&1; then
    printf 'default\t%s\n' myproject
    return 0
  fi
  printf 'default\t\n'
}

_hpclib_quota_set() {  # _hpclib_quota_set storage|project COMMAND|default|none
  local kind="$1" value="$2" file var storage project tmp
  case "$kind" in
    storage|project) ;;
    *) echo "hpclib_quota set storage|project COMMAND (or default, or none)" >&2; return 2 ;;
  esac
  case "$value" in
    *$'\n'*|*$'\r'*) echo "hpclib_quota: a check is one line" >&2; return 2 ;;
  esac
  [ "${#value}" -le 1000 ] || { echo "hpclib_quota: a check is at most 1000 characters" >&2; return 2; }
  [ "$value" = default ] && value=''
  file=$(_hpclib_quota_settings_file)
  storage='' project=''
  if [ -f "$file" ]; then
    storage=$(unset HPCLIB_STORAGE_QUOTA_CHECK; . "$file" > /dev/null 2>&1; printf '%s' "${HPCLIB_STORAGE_QUOTA_CHECK:-}")
    project=$(unset HPCLIB_PROJECT_QUOTA_CHECK; . "$file" > /dev/null 2>&1; printf '%s' "${HPCLIB_PROJECT_QUOTA_CHECK:-}")
  fi
  if [ "$kind" = storage ]; then storage="$value"; else project="$value"; fi
  mkdir -p "$(dirname "$file")" || return 1
  tmp=$(mktemp "$file.XXXXXX") || return 1
  {
    echo "# hpclib's quota checks (hpclib_quota set, or the console's cluster Settings); environment variables win"
    if [ -n "$storage" ]; then printf 'HPCLIB_STORAGE_QUOTA_CHECK=%q\n' "$storage"; fi
    if [ -n "$project" ]; then printf 'HPCLIB_PROJECT_QUOTA_CHECK=%q\n' "$project"; fi
  } > "$tmp" && chmod 600 "$tmp" && mv -f "$tmp" "$file"
}

_hpclib_quota_saved() {  # _hpclib_quota_saved storage|project: what the settings file says (or nothing)
  local var file
  var="HPCLIB_$(printf '%s' "$1" | tr '[:lower:]' '[:upper:]')_QUOTA_CHECK"
  file=$(_hpclib_quota_settings_file)
  [ -f "$file" ] && (unset "$var"; . "$file" > /dev/null 2>&1; printf '%s' "${!var:-}")
}

_hpclib_quota_run() {  # _hpclib_quota_run KIND JSON(yes/no): one check
  local kind="$1" json="$2" line source command out code saved
  saved=$(_hpclib_quota_saved "$kind")
  line=$(_hpclib_quota_command "$kind")
  source="${line%%$'\t'*}"
  command="${line#*$'\t'}"
  if [ "$command" = none ] || [ -z "$command" ]; then
    [ "$command" = none ] && source=none
    command=''
    code=''
    out=''
  else
    out=$(timeout 60 bash -c "$command" 2>&1 < /dev/null; echo "HPCLIB_QUOTA_EXIT $?")
    code="${out##*HPCLIB_QUOTA_EXIT }"
    out="${out%HPCLIB_QUOTA_EXIT *}"
    out=$(printf '%s' "$out" | head -n 200)
  fi
  if [ "$json" = yes ]; then
    printf '%s\0%s\0%s\0%s\0%s\0%s\0' "$kind" "$source" "$command" "$code" "$out" "$saved"
  else
    if [ -z "$command" ]; then
      echo "== $kind quota: no check $( [ "$source" = none ] && echo '(turned off)' || echo '(none found on this cluster; set one with hpclib_quota set)')"
    else
      echo "== $kind quota: $command ($source)$( [ "$code" != 0 ] && echo ", exit $code")"
      printf '%s\n' "$out"
    fi
  fi
}

hpclib_quota() {
  local kinds=(storage project) json=no a
  case "${1:-}" in
    set) shift; _hpclib_quota_set "$@"; return ;;
    -h|--help) sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; return 0 ;;
  esac
  for a in "$@"; do
    case "$a" in
      storage|project) kinds=("$a") ;;
      all) kinds=(storage project) ;;
      --json) json=yes ;;
      *) echo "usage: hpclib_quota [storage|project|all] [--json] | hpclib_quota set storage|project COMMAND" >&2
         return 2 ;;
    esac
  done
  if [ "$json" = no ]; then
    for a in "${kinds[@]}"; do _hpclib_quota_run "$a" no; done
    return 0
  fi
  for a in "${kinds[@]}"; do _hpclib_quota_run "$a" yes; done | python3 -c '
import json, os, socket, sys
f = sys.stdin.buffer.read().decode(errors="replace").split("\0")
out = {"host": socket.gethostname(), "settings_file": sys.argv[1]}
for i in range(0, len(f) - 5, 6):
    kind, source, command, code, text, saved = f[i:i + 6]
    out[kind] = {"command": command or None, "source": source, "exit_code": int(code) if code.isdigit() else None,
                 "output": text.splitlines()[:200], "saved": saved or None}
print("HPCLIB_QUOTA " + json.dumps(out))
' "$(_hpclib_quota_settings_file)"
}

# From your machine: the checks on a cluster, over ssh (this file goes along, so it works before hpclib is
# installed there). Your HPCLIB_*_QUOTA_CHECK, when set, is used there.
cluster_quota() {
  local usage='usage: cluster_quota [storage|project|all] [--json] [ssh options] [user@]host'
  local args=() login_args=() env_lines='' v
  while [ "$#" -gt 0 ]; do
    case "$1" in
      storage|project|all|--json) args+=("$1"); shift ;;
      -h|--help) echo "$usage"; return 0 ;;
      *) login_args+=("$1"); shift ;;
    esac
  done
  [ "${#login_args[@]}" -gt 0 ] || { echo "$usage" >&2; return 2; }
  for v in HPCLIB_STORAGE_QUOTA_CHECK HPCLIB_PROJECT_QUOTA_CHECK; do
    [ -n "${!v:-}" ] && env_lines+="export $v=$(printf '%q' "${!v}")"$'\n'
  done
  { printf '%s' "$env_lines"
    declare -f _hpclib_quota_settings_file _hpclib_quota_command _hpclib_quota_set _hpclib_quota_saved \
      _hpclib_quota_run hpclib_quota
    echo 'hpclib_quota "$@"'
  } | HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" "$(printf '%q ' bash -s -- "${args[@]}")"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  hpclib_quota "$@"
fi
