#!/usr/bin/env bash
# smbshell: files to and from an SMB server with Kerberos and smbclient, from the data-transfer-tools image, on a cluster's login
# node, or in a SLURM job (submit). From your own machine: smbshell --on [ssh options] user@host COMMAND ...
#
#   smbshell ls [REMOTE] [--json] [--dry-run]    list (--json: structured entries; no REMOTE: SMB_ROOT, or the shares)
#   smbshell get REMOTE LOCAL [--dry-run]        copy from the server (smbclient)
#   smbshell put LOCAL REMOTE [--dry-run]        copy to the server
#   smbshell sync pull REMOTE LOCAL [--dry-run]  make LOCAL match REMOTE (deletes what REMOTE lacks)
#   smbshell sync push LOCAL REMOTE [--dry-run]  make REMOTE match LOCAL (likewise; try --dry-run first)
#   smbshell submit [sbatch options] get|put|sync ...   the same, as a SLURM job (--time=..., --mem=...)
#   smbshell submit [sbatch options] --manifest FILE.json   one job per entry, as an array: a JSON list of
#                                                   argument lists, e.g. [["get", "proj/raw", "/scratch/.../raw"]]
#   smbshell jobs                                   your transfer jobs in the queue
#   smbshell login | logout                         a Kerberos ticket for the server (kinit / kdestroy)
#   smbshell check                                  test the configured folder once, without transferring files
#   smbshell find-spn                               which name the server has in Kerberos (for SMB_SPN), when
#                                                   cifs/SMB_HOST is "not found in Kerberos database"
#   smbshell forget-credentials                    remove a password saved by an older version
#   smbshell status [--json [--brief]]              the image, and how it would sign in (--brief: without asking
#                                                   the image about smbclient, for quick checks)
#   smbshell action-get|action-put --scope F --helper H ...   what an agent's tunnel action runs in its job
#                                                   (actions.json here; see hpclib/servers/rest_actions.py)
#   smbshell shell                                  a shell in the image, with the transfer ticket
#   smbshell gui --port PORT                        a web GUI on 127.0.0.1:PORT of this node, signed in for this
#                                                   session only (the console's Data transfer page forwards it to
#                                                   your browser): rclone's (rclone rcd --rc-web-gui), or the one
#                                                   SMB_GUI names (see "A GUI of your own" below)
#
# REMOTE is a path under SMB_ROOT (e.g. research/our_group) when that is set, else SHARE/PATH on
# SMB_HOST; /SHARE/PATH and //HOST/SHARE/PATH are absolute. CLI and jobs require a ticket from `smbshell login`
# or an optional GUI sign-in. The GUI asks once and creates the ticket automatically. Settings: settings.sh here.
#
# A GUI of your own (SMB_GUI=NAME, for NAME.gui.sh in ~/.local/tunnels/settings/data-transfer.d/, where a settings
# package puts it; or SMB_GUI=/path/to/program): `smbshell gui` signs in, then runs it (with bash, unless it is an
# executable path) on this node with
#   HPCLIB_GUI_PORT    the port to serve on, at 127.0.0.1 only
#   HPCLIB_GUI_USER    a login made for this session (random): require it, since anyone on this node can reach
#   HPCLIB_GUI_PASS    127.0.0.1:PORT
#   HPCLIB_GUI_DIR     a private directory in memory, removed when it stops
#   RCLONE_CONFIG      rclone's remotes, signed in: smb (the server; with SMB_ROOT, that folder, and smb-server the
#                      whole server), cluster (this node's files), and the extras
#   SMBSHELL_RCLONE    rclone in the image with that config: "$SMBSHELL_RCLONE" lsjson smb:SHARE/DIR
# It prints one line on standard output once it serves, saying where to open it:
#   HPCLIB_TRANSFER_GUI port=PORT [path=/start] [user=USER pass=PASS] [query=a=1&b=2]   (values without spaces)
# and runs until it gets TERM (Stop, or the session ending).

set -o pipefail
umask 077
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/settings.sh"
runtime=$(command -v singularity || command -v apptainer || true)
logdir="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/sessions/data-transfer"
binds=()
kerberos_binds=()
image_environment=()
image_prepared=false

die() { echo "smbshell: $*" >&2; exit 1; }
usage() { sed -n '2,/^$/p' "$here/smbshell.sh" | sed 's/^# \{0,1\}//'; }   # the comment above

need_image() {
  [ -n "$runtime" ] || die "neither singularity nor apptainer is on this node's PATH"
  [ -f "$SMB_IMAGE" ] || die "no image at $SMB_IMAGE; install it (setup_tunnel.sh data-transfer --install, or the console's Install)"
}

# KCM is a host service, and rclone cannot read KCM caches. The app always uses
# its own FILE cache, regardless of an inherited KRB5CCNAME=KCM:. Do not assume
# that the container's default home/tmp mounts expose a custom cache location.
case "$SMB_KRB5CCNAME" in
  FILE:*) SMB_KRB5CCNAME="${SMB_KRB5CCNAME#FILE:}" ;;
  KCM:*|KEYRING:*|DIR:*|MEMORY:*) die "SMB_KRB5CCNAME must name a FILE cache; sign in with smbshell login" ;;
esac
case "$SMB_KRB5CCNAME" in ''|*/) die "SMB_KRB5CCNAME must name a ticket file, not a directory" ;; esac
[[ "$SMB_KRB5CCNAME" = /* ]] || SMB_KRB5CCNAME="$PWD/$SMB_KRB5CCNAME"
[ ! -d "$SMB_KRB5CCNAME" ] || die "SMB_KRB5CCNAME must name a ticket file, not a directory"
export SMB_KRB5CCNAME
krb5_conf=''
host_krb5_config="${SMB_KRB5_CONF:-${KRB5_CONFIG:-}}"

cache_directory() {
  mkdir -p -m 700 "$(dirname "$SMB_KRB5CCNAME")" || die "cannot create the transfer ticket directory"
  [ ! -L "$SMB_KRB5CCNAME" ] || die "the transfer ticket cache must not be a symlink"
}

prepare_image() {
  [ "$image_prepared" = true ] && return
  cache_directory
  local source profile_error sources=() profile_args=()
  if [ -n "$host_krb5_config" ]; then
    IFS=: read -r -a sources <<< "$host_krb5_config"
  elif [ -f /etc/krb5.conf ]; then
    sources=(/etc/krb5.conf)
  fi
  for source in "${sources[@]}"; do profile_args+=(--source "$source"); done
  krb5_conf="$SMB_KRB5CCNAME.krb5.conf"
  if ! profile_error=$(python3 "$here/kerberos_config.py" "${profile_args[@]}" --output "$krb5_conf" \
    --cache "$SMB_KRB5CCNAME" --realm "${SMB_REALM:-}" 2>&1); then
    # The console reports the last stderr line. Keep the actual cause on it.
    profile_error=${profile_error//$'\n'/ }
    die "Kerberos configuration is unavailable: ${profile_error#smbshell: }"
  fi
  kerberos_binds=(--bind "$(dirname "$SMB_KRB5CCNAME"):$(dirname "$SMB_KRB5CCNAME"):rw" \
                 --bind "$krb5_conf:/etc/hpclib-krb5.conf:ro")
  # Explicit runtime overrides also replace values baked into the image. Keep
  # native host tools on the host configuration and its KCM daemon.
  image_environment=(env "KRB5CCNAME=FILE:$SMB_KRB5CCNAME" KRB5_CONFIG=/etc/hpclib-krb5.conf \
    "SINGULARITYENV_KRB5CCNAME=FILE:$SMB_KRB5CCNAME" SINGULARITYENV_KRB5_CONFIG=/etc/hpclib-krb5.conf \
    "APPTAINERENV_KRB5CCNAME=FILE:$SMB_KRB5CCNAME" APPTAINERENV_KRB5_CONFIG=/etc/hpclib-krb5.conf)
  image_prepared=true
}

image_arguments() {
  # Password transfers and capability probes do not need a Kerberos profile.
  # Once prepared for a GUI, retain its environment even after its remote
  # settings have moved from environment variables into the GUI's config file.
  case "$1" in
    kinit|klist|kvno|kdestroy) prepare_image ;;
    *) if [ "$image_prepared" = true ] || [ "${RCLONE_CONFIG_SMB_USE_KERBEROS:-}" = true ]; then
         prepare_image
       else
         image_environment=(env)
         kerberos_binds=()
       fi ;;
  esac
}

img() {  # img COMMAND...: run it in the image, with the binds asked for
  need_image
  image_arguments "$1"
  "${image_environment[@]}" "$runtime" exec "${binds[@]}" "${kerberos_binds[@]}" "$SMB_IMAGE" "$@"
}

img_exec() {  # img_exec COMMAND...: img, replacing this (sub)shell, so its pid is the image runtime's
  need_image
  image_arguments "$1"
  exec "${image_environment[@]}" "$runtime" exec "${binds[@]}" "${kerberos_binds[@]}" "$SMB_IMAGE" "$@"
}

bind_local() {
  local path dir
  path=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1") || die "invalid local path"
  dir="$path"
  while [ ! -e "$dir" ] && [ ! -L "$dir" ]; do dir=$(dirname "$dir"); done
  binds+=(--bind "$dir")
  LOCAL="$path"
}

remote() {  # remote PATH: rclone's name for it (sets REMOTE, maybe SMB_HOST). PATH is relative to SMB_ROOT (a
            # SHARE/FOLDER) when that is set, or SHARE/PATH when not; /SHARE/PATH and //HOST/SHARE/PATH are absolute
  local p="$1"
  case "$p" in
    //*) p="${p#//}"; SMB_HOST="${p%%/*}"; case "$p" in */*) p="${p#*/}" ;; *) p="" ;; esac ;;
    smb:*) p="${p#smb:}" ;;
    /*) p="${p#/}" ;;
    *) if [ -n "${SMB_ROOT:-}" ]; then p="${SMB_ROOT%/}${p:+/$p}"; p="${p#/}"; fi ;;
  esac
  case "$p" in .|./) p="" ;; esac
  REMOTE="smb:${p%/}"
}

# -- Kerberos --------------------------------------------------------------------------------------

krb() {  # krb kinit|klist|kdestroy ARGS: on this node if it has them, else in the image; the ticket cache is
         # SMB_KRB5CCNAME, in your home directory, so jobs see it
  local tool="$1"
  shift
  cache_directory
  if command -v "$tool" > /dev/null 2>&1; then
    if [ -n "$host_krb5_config" ]; then
      KRB5_CONFIG="$host_krb5_config" KRB5CCNAME="FILE:$SMB_KRB5CCNAME" "$tool" "$@"
    else
      KRB5CCNAME="FILE:$SMB_KRB5CCNAME" "$tool" "$@"
    fi
  else
    KRB5CCNAME="FILE:$SMB_KRB5CCNAME" img "$tool" "$@"
  fi
}
kinit_where() {  # host, image, or nothing
  if command -v kinit > /dev/null 2>&1; then echo host
  elif [ -n "$runtime" ] && [ -f "$SMB_IMAGE" ] && img sh -c 'command -v kinit' > /dev/null 2>&1; then echo image
  fi
}
has_ticket() { [ -f "$SMB_KRB5CCNAME" ] && krb klist -s > /dev/null 2>&1; }

login() (
  [ -n "$(kinit_where)" ] || die "no kinit here or in the image"
  cache_directory
  local candidate supplied=false
  if [ "${1:-}" = --password-stdin ]; then supplied=true; shift; fi
  candidate=$(mktemp "$(dirname "$SMB_KRB5CCNAME")/.krb5-login.XXXXXX") || die "cannot create a private ticket cache"
  trap 'rm -f "$candidate" "$candidate.krb5.conf"' EXIT
  # A failed password/renewal must not destroy a ticket used by a running GUI or
  # queued job. kinit writes only the candidate, which replaces the cache on success.
  if (SMB_KRB5CCNAME="$candidate"; image_prepared=false
      if [ "$supplied" = true ]; then
        krb kinit "${SMB_USER%@*}${SMB_REALM:+@$SMB_REALM}" "$@" > /dev/null
      else
        krb kinit "${SMB_USER%@*}${SMB_REALM:+@$SMB_REALM}" "$@"
      fi && krb klist -s); then
    chmod 600 "$candidate" && mv -f "$candidate" "$SMB_KRB5CCNAME" || die "cannot publish the transfer ticket"
  else
    rm -f "$candidate"
    die "kinit failed; the previous transfer ticket was kept"
  fi
  echo "signed in for transfers: $(krb klist 2>/dev/null | sed -n 's/^Default principal: *//p')"
  krb klist 2>/dev/null | grep -i -m 1 krbtgt
)

# -- signing in -------------------------------------------------------------------------------------

obscure() { img rclone --config=/dev/null obscure -; }   # session password on stdin; no saved config

sign_in() {
  [ -n "${SMB_HOST:-}" ] || die "set SMB_HOST first"
  unset RCLONE_CONFIG_SMB_PASS RCLONE_CONFIG_SMB_USE_KERBEROS RCLONE_CONFIG_SMB_SPN RCLONE_CONFIG_SMB_DOMAIN
  has_ticket || die "no transfer ticket (or it has expired): sign in from Data transfer, or run smbshell login"
}

samba_transfer() {
  need_image
  prepare_image
  local ticket_principal options=("${binds[@]}")
  ticket_principal=$(LC_ALL=C krb klist 2>/dev/null | sed -n 's/^Default principal: *//p' | head -n 1)
  "${image_environment[@]}" python3 "$here/smbclient_transfer.py" \
    --runtime "$runtime" --image "$SMB_IMAGE" --cache "$SMB_KRB5CCNAME" --profile "$krb5_conf" \
    --ticket-principal "$ticket_principal" \
    "${options[@]}" "$@"
}

check_login() {
  local error
  if ! error=$(samba_transfer check "${REMOTE#smb:}" 2>&1); then
    error=${error//$'\n'/ }
    die "could not open the configured folder with Kerberos: ${error#smbshell: }"
  fi
}

gui_sign_in() {
  [ -n "${SMB_HOST:-}" ] || die "set SMB_HOST first"
  [ -t 0 ] && [ -z "${SMB_NONINTERACTIVE:-}" ] || die "opening the optional GUI requires an interactive sign-in"
  local pw
  IFS= read -rsp "Password for $SMB_USER (GUI session and transfer sign-in): " pw
  echo >&2
  [ -n "$pw" ] || die "no password given"
  printf '%s\n' "$pw" | login --password-stdin || { unset pw; die "GUI not started: transfer sign-in failed"; }
  unset RCLONE_CONFIG_SMB_PASS RCLONE_CONFIG_SMB_USE_KERBEROS RCLONE_CONFIG_SMB_SPN RCLONE_CONFIG_SMB_DOMAIN
  export RCLONE_CONFIG_SMB_TYPE=smb RCLONE_CONFIG_SMB_HOST="$SMB_HOST" RCLONE_CONFIG_SMB_USER="$SMB_USER"
  if [ -n "${SMB_DOMAIN:-}" ]; then
    export RCLONE_CONFIG_SMB_DOMAIN="$SMB_DOMAIN"
  elif [[ "$SMB_USER" == *@* ]]; then
    export RCLONE_CONFIG_SMB_USER="${SMB_USER%@*}" RCLONE_CONFIG_SMB_DOMAIN="${SMB_USER#*@}"
  fi
  RCLONE_CONFIG_SMB_PASS=$(printf '%s' "$pw" | obscure) || { unset pw; die "could not prepare the GUI session"; }
  unset pw
  export RCLONE_CONFIG_SMB_USE_KERBEROS=false RCLONE_CONFIG=/dev/null
}

transfer() {
  local cmd="$1"; shift
  case "$cmd" in
    ls)
      if [ "$#" -ge 1 ] && [ "${1#-}" = "$1" ]; then remote "$1"; shift; else remote ""; fi
      sign_in
      samba_transfer ls "${REMOTE#smb:}" "$@" ;;
    get)
      [ "$#" -ge 2 ] || die "usage: smbshell get REMOTE LOCAL [--dry-run]"
      remote "$1"; bind_local "$2"; shift 2
      sign_in
      samba_transfer get "${REMOTE#smb:}" "$LOCAL" "$@" ;;
    put)
      [ "$#" -ge 2 ] || die "usage: smbshell put LOCAL REMOTE [--dry-run]"
      [ -e "$1" ] || die "no $1"
      bind_local "$1"; remote "$2"; shift 2
      sign_in
      samba_transfer put "${REMOTE#smb:}" "$LOCAL" "$@" ;;
    sync)
      [ "$#" -ge 3 ] || die "usage: smbshell sync pull REMOTE LOCAL | sync push LOCAL REMOTE [--dry-run]"
      local way="$1"; shift
      case "$way" in
        pull) remote "$1"; bind_local "$2" ;;
        push) [ -e "$1" ] || die "no $1"; bind_local "$1"; remote "$2" ;;
        *) die "sync pull REMOTE LOCAL, or sync push LOCAL REMOTE" ;;
      esac
      shift 2
      sign_in
      samba_transfer "sync-$way" "${REMOTE#smb:}" "$LOCAL" "$@" ;;
    *) die "unknown transfer $cmd" ;;
  esac
}

submit() {
  # sbatch options first, then the transfer (or --manifest FILE)
  local opts=() manifest='' n
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --manifest) manifest="$2"; shift 2 ;;
      --manifest=*) manifest="${1#--manifest=}"; shift ;;
      -*) opts+=("$1"); shift ;;
      *) break ;;
    esac
  done
  # a job can't ask for a password
  if has_ticket; then
    echo "smbshell: the job uses your Kerberos ticket; it must outlive the job:" >&2
    krb klist 2>/dev/null | grep -i -m 1 krbtgt | sed 's/^/  /' >&2 || true
  else
    die "a job needs a transfer ticket: sign in through Data transfer or run smbshell login"
  fi
  [ -n "${SMB_HOST:-}" ] || die "set SMB_HOST first"
  need_image
  mkdir -p "$logdir"
  local defaults=""
  if [ -f "$here/tunnel_config.sh" ]; then defaults=$(. "$here/tunnel_config.sh"; printf '%s' "$DEFAULT_SBATCH_ARGS"); fi
  if [ -n "$manifest" ]; then
    [ -f "$manifest" ] || die "no manifest $manifest"
    local lines="$logdir/manifest-$(date +%Y%m%d-%H%M%S)-$$.jsonl"
    n=$(python3 - "$manifest" "$lines" <<'PY'
import json, sys
entries = json.load(open(sys.argv[1]))
if not isinstance(entries, list) or not entries:
    sys.exit("smbshell: the manifest must be a non-empty JSON list of argument lists")
with open(sys.argv[2], "w") as out:
    for i, e in enumerate(entries):
        if not (isinstance(e, list) and e and all(isinstance(a, str) for a in e) and e[0] in ("get", "put", "sync")):
            sys.exit(f"smbshell: manifest entry {i} must be a list like [\"get\", \"SHARE/PATH\", \"/local/path\"]")
        out.write(json.dumps(e) + "\n")
print(len(entries))
PY
    ) || { rm -f "$lines"; exit 1; }
    SMBSHELL_DIR="$here" sbatch --parsable --job-name=smb-transfer --array="0-$((n - 1))" \
      --output="$logdir/transfer-%A_%a.log" $defaults "${opts[@]}" "$here/sbatch_script.sh" --manifest "$lines"
  else
    case "${1:-}" in get|put|sync|action-get|action-put) ;; *) die "submit get|put|sync ..., or submit --manifest FILE" ;; esac
    SMBSHELL_DIR="$here" sbatch --parsable --job-name=smb-transfer --output="$logdir/transfer-%j.log" \
      $defaults "${opts[@]}" "$here/sbatch_script.sh" "$@"
  fi
}

# -- tunnel actions --------------------------------------------------------------------------------
# An agent's action (actions.json) runs these in its job, as you: the paths it names are in the agent's directories,
# which the agent can change while the job waits. The SMB client never sees them: it copies to or from a staging
# directory of ours, and action_paths.py (--helper) moves files between that and the agent's path without
# following links, checking the path against the token's directories (--scope) when the job runs.
action_args() {  # sets SCOPE HELPER, and ARGS to the rest
  SCOPE='' HELPER=''
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --scope) SCOPE="$2"; shift 2 ;;
      --helper) HELPER="$2"; shift 2 ;;
      *) break ;;
    esac
  done
  [ -f "$SCOPE" ] && [ -f "$HELPER" ] || die "an action needs --scope FILE --helper action_paths.py"
  ARGS=("$@")
}
action_staging() {
  mkdir -p -m 700 "$logdir/actions"
  STAGING=$(mktemp -d "$logdir/actions/staging-${SLURM_JOB_ID:-$$}-XXXXXX") || die "no staging directory"
  trap 'rm -rf "$STAGING"; rm -f "$SCOPE"' EXIT
}
action_get() {  # action-get --scope F --helper H REMOTE DEST: into DEST, a new directory
  action_args "$@"
  [ "${#ARGS[@]}" -eq 2 ] || die "usage: smbshell action-get --scope F --helper H REMOTE DEST"
  action_staging
  ( transfer get "${ARGS[0]}" "$STAGING/data" ) || exit 1
  python3 "$HELPER" place "$STAGING/data" "${ARGS[1]}" --scope "$SCOPE" || exit 1
  echo "smbshell: copied ${ARGS[0]} into ${ARGS[1]}"
}
action_put() {  # action-put --scope F --helper H SRC REMOTE: SRC (a file or directory) to REMOTE
  action_args "$@"
  [ "${#ARGS[@]}" -eq 2 ] || die "usage: smbshell action-put --scope F --helper H SRC REMOTE"
  action_staging
  python3 "$HELPER" stage-in "${ARGS[0]}" "$STAGING" --scope "$SCOPE" || exit 1
  ( transfer put "$STAGING/$(basename "${ARGS[0]}")" "${ARGS[1]}" ) || exit 1
  echo "smbshell: copied ${ARGS[0]} to ${ARGS[1]}"
}

status() {
  local where ticket='' principal='' expires='' creds=false image=false kerb_rclone=false samba=false
  local host_principal='' host_expires='' host_cache=''
  where=$(kinit_where)
  [ -f "$SMB_IMAGE" ] && image=true
  [ -f "$SMB_CREDENTIALS" ] && creds=true
  if [ -n "$where" ] && has_ticket; then
    ticket=true
    principal=$(krb klist 2>/dev/null | sed -n 's/^Default principal: *//p' | head -n 1)
    expires=$(krb klist 2>/dev/null | grep -i -m 1 krbtgt | awk '{print $3, $4}')
  fi
  # An ordinary host kinit may have a valid KCM ticket that the image cannot use.
  # Report it separately; neither UI nor agents may mistake it for a transfer ticket.
  if [ -z "$ticket" ] && command -v klist >/dev/null 2>&1 && klist -s >/dev/null 2>&1; then
    host_principal=$(klist 2>/dev/null | sed -n 's/^Default principal: *//p' | head -n 1)
    host_expires=$(klist 2>/dev/null | grep -i -m 1 krbtgt | awk '{print $3, $4}')
    host_cache=$(klist 2>/dev/null | sed -n 's/^Ticket cache: *//p' | head -n 1)
  fi
  if [ "${2:-}" != --brief ] && [ "$image" = true ] && [ -n "$runtime" ] &&
     img smbclient --version > /dev/null 2>&1; then samba=true; fi
  if [ "${1:-}" = --json ]; then
    python3 - "$where" "$ticket" "$principal" "$expires" "$creds" "$image" "$kerb_rclone" \
      "$host_principal" "$host_expires" "$host_cache" "$samba" <<'PY'
import json, os, sys, time
where, ticket, principal, expires, creds, image, kerb, host_principal, host_expires, host_cache, samba = sys.argv[1:]
print("HPCLIB_SMB_STATUS " + json.dumps({
    "host": os.environ.get("SMB_HOST") or None, "root": os.environ.get("SMB_ROOT") or None,
    "user": os.environ.get("SMB_USER"),
    "auth": os.environ.get("SMB_AUTH"), "kinit": where or None, "rclone_kerberos": kerb == "true",
    "ticket": {"principal": principal, "expires": expires} if ticket == "true" else None,
    "ticket_cache": "FILE", "host_ticket": {"principal": host_principal, "expires": host_expires,
                                             "cache": host_cache} if host_principal else None,
    "observed_at": time.time(), "transfer_backend": "smbclient", "smbclient": samba == "true",
    "saved_credentials": creds == "true", "credentials": False, "image": image == "true", "image_path": os.environ.get("SMB_IMAGE")}))
PY
    return
  fi
  echo "server:      ${SMB_HOST:-(not set: SMB_HOST)} as $SMB_USER${SMB_DOMAIN:+ ($SMB_DOMAIN)}"
  echo "folder:      ${SMB_ROOT:-(none: paths are SHARE/PATH)}"
  echo "image:       $SMB_IMAGE ($([ "$image" = true ] && echo present || echo missing))"
  echo "kinit:       ${where:-not found}; CLI and agents use smbclient"
  echo "ticket:      $([ -n "$ticket" ] && echo "$principal until $expires" || echo none)"
  [ -z "$host_principal" ] || echo "host ticket: $host_principal ($host_cache); use smbshell login to sign in for transfers"
  echo "saved password: $([ "$creds" = true ] && echo "yes ($SMB_CREDENTIALS)" || echo no)"
}

cmd="${1:-}"
[ -n "$cmd" ] && shift
case "$cmd" in
  ''|-h|--help|help) usage ;;
  ls|get|put|sync) transfer "$cmd" "$@" ;;
  check)
    export SMB_NONINTERACTIVE=1
    remote ""; sign_in; check_login
    echo "HPCLIB_SMB_CHECK OK"
    echo "Connection verified: $SMB_HOST ${REMOTE#smb:}" ;;
  submit) submit "$@" ;;
  action-get) action_get "$@" ;;
  action-put) action_put "$@" ;;
  jobs) squeue -u "$(id -un)" --name=smb-transfer -o "%.12i %.10T %.10M %.12l %R" ;;
  login) login "$@" ;;
  logout) krb kdestroy 2>/dev/null; rm -f "$SMB_KRB5CCNAME"; echo "Kerberos ticket removed" ;;
  find-spn)
    # rclone asks the KDC for cifs/SMB_HOST exactly; MIT's tools (smbclient) also try the name DNS gives back
    # for it. Try those names, as kvno would get a ticket for them, and say which the KDC knows.
    [ -n "${SMB_HOST:-}" ] || die "set SMB_HOST first"
    has_ticket || die "no Kerberos ticket: run smbshell login first"
    names=$(python3 - "$SMB_HOST" <<'PY'
import socket, sys
host = sys.argv[1]
seen = []
def add(n):
    n = (n or "").rstrip(".")
    for m in (n, n.lower()):
        if m and m not in seen:
            seen.append(m)
add(host)
try:
    for *_, canon, addr in socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM, 0, socket.AI_CANONNAME):
        add(canon)
        try:
            name, aliases, _ = socket.gethostbyaddr(addr[0])
            add(name)
            for a in aliases:
                add(a)
        except OSError:
            pass
except OSError:
    pass
for n in list(seen):
    if "." in n and not n.replace(".", "").isdigit():
        add(n.split(".")[0])
        add(n.split(".")[0].upper())
print("\n".join(seen))
PY
    )
    found=''
    for name in $names; do
      if error=$(krb kvno "cifs/$name" 2>&1); then
        echo "known:     cifs/$name"
        [ -z "$found" ] && found="cifs/$name"
      elif printf '%s' "$error" | grep -qiE 'server not found in Kerberos database|KDC_ERR_S_PRINCIPAL_UNKNOWN'; then
        echo "not known: cifs/$name"
      else
        die "could not test cifs/$name: $error"
      fi
    done
    if [ -n "$found" ]; then
      echo "use it:    setup_tunnel.sh data-transfer --set SMB_SPN=$found --save (or the console's SPN setting)"
    else
      echo "none of these is known to the KDC; ask the server's admins for its CIFS service principal name" >&2
      exit 1
    fi ;;
  save-credentials) die "passwords are no longer saved; use smbshell login, or the optional GUI session" ;;
  forget-credentials) rm -f "$SMB_CREDENTIALS"; echo "saved password removed" ;;
  status) status "$@" ;;
  shell)
    sign_in
    need_image
    prepare_image
    echo "smbshell: this shell has the transfer ticket; use smbclient with --use-kerberos=required --no-pass" >&2
    exec "${image_environment[@]}" "$runtime" shell "${binds[@]}" "${kerberos_binds[@]}" "$SMB_IMAGE" ;;
  rclone) die "rclone SMB is reserved for the optional password GUI; use smbshell ls/get/put/sync for ticket-based transfers" ;;
  gui)
    # rclone's web GUI on this (login) node, for as long as this runs. Its remotes are in a config file in a
    # private directory in memory (/dev/shm or XDG_RUNTIME_DIR), removed when it stops: smb (the server; with
    # SMB_ROOT set, that folder, as for the other commands, and smb-server the whole server), cluster (this
    # node's files), and any in $HPCTUNNELS_DATA_DIR/settings/data-transfer.d/rclone.conf (which a settings
    # package installs, e.g. an alias for a group's folder: [ours] type = alias remote = smb:SHARE/DIR; their
    # smb: means the whole server, as it always has).
    # Its own login is a random user and password, printed once for the console to open it with.
    port=''
    while [ "$#" -gt 0 ]; do
      case "$1" in
        --port) port="$2"; shift 2 ;;
        --port=*) port="${1#--port=}"; shift ;;
        *) die "usage: smbshell gui --port PORT" ;;
      esac
    done
    case "$port" in ''|*[!0-9]*) die "usage: smbshell gui --port PORT" ;; esac
    # which GUI (SMB_GUI): rclone's own, or a program of yours, found before asking for a password
    gui="${SMB_GUI:-rclone}"
    gui_prog=''
    case "$gui" in
      rclone) ;;
      */*) gui_prog="$gui" ;;
      *[!A-Za-z0-9_.-]*) die "SMB_GUI is rclone, a NAME (letters, digits, _ . -) or a path; not $gui" ;;
      *) gui_prog="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/settings/data-transfer.d/$gui.gui.sh" ;;
    esac
    [ -z "$gui_prog" ] || [ -f "$gui_prog" ] ||
      die "no GUI $gui: $gui_prog doesn't exist (SMB_GUI: rclone, NAME for data-transfer.d/NAME.gui.sh, or a program)"
    # marked as hpclib's for this port (see _hpclib_port_holders), and the port cleared of what an earlier GUI
    # or tunnel of yours left on it, before asking for a password
    export HPCLIB_TUNNEL_PORT="$port"
    hpclib_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../lib" 2> /dev/null && pwd)/tunnels.sh"
    if [ -f "$hpclib_lib" ]; then
      # shellcheck source=/dev/null
      . "$hpclib_lib"
      _hpclib_clear_port "$port" || die "port $port is busy; pick another"
    fi
    need_image
    unset RCLONE_CONFIG_SMB_USE_KERBEROS
    img rclone --config=/dev/null version > /dev/null 2>&1 || die "the optional GUI needs rclone in the image; ticket-based transfers use smbclient"
    memory_dir=''
    for candidate_dir in "${XDG_RUNTIME_DIR:-}" /dev/shm; do
      [ -n "$candidate_dir" ] && [ -d "$candidate_dir" ] || continue
      case "$(stat -f -c %T "$candidate_dir" 2>/dev/null)" in
        tmpfs|ramfs) memory_dir="$candidate_dir"; break ;;
      esac
    done
    [ -n "$memory_dir" ] || die "the GUI needs a memory filesystem for its session password (XDG_RUNTIME_DIR or /dev/shm)"
    run_dir=$(mktemp -d "$memory_dir/hpclib-rclone-gui.XXXXXX") || die "no private directory for the GUI session"
    chmod 700 "$run_dir"
    # rclone runs as a child and is stopped with this script, whichever way it ends (the console's Stop, a closed
    # or dropped ssh session), so it can't stay behind holding the port
    gui_pid=''
    gui_stop() {
      trap '' HUP INT TERM
      if [ -n "$gui_pid" ] && kill -0 "$gui_pid" 2> /dev/null; then
        # the image's runtime, which passes the signal on to rclone and ends with it
        kill -TERM "$gui_pid" 2> /dev/null
        for _ in 1 2 3 4 5 6 7 8 9 10; do kill -0 "$gui_pid" 2> /dev/null || break; sleep 0.5; done
        kill -KILL "$gui_pid" 2> /dev/null
      fi
      rm -rf "$run_dir"
    }
    trap gui_stop EXIT
    trap 'exit 129' HUP INT TERM
    gui_sign_in
    status --json --brief
    conf="$run_dir/rclone.conf"
    extra_remotes="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/settings/data-transfer.d/rclone.conf"
    extra_names=$( [ -f "$extra_remotes" ] && sed -n 's/^\[\(.*\)\][[:space:]]*$/\1/p' "$extra_remotes" | tr '\n' ' ')
    case " $extra_names " in
      *" smb "*|*" smb-server "*|*" cluster "*)
        die "$extra_remotes may not define smb, smb-server or cluster, which smbshell makes" ;;
    esac
    # smb opens at the settings' folder (SMB_ROOT, a SHARE/FOLDER), like smbshell's other commands; the server
    # itself is then smb-server, and the extra remotes' smb: (written for the whole server) points there
    root="${SMB_ROOT:-}"
    root="${root#/}"; root="${root%/}"
    server_remote=smb
    [ -n "$root" ] && server_remote=smb-server
    (
      umask 077
      printf '[%s]\ntype = smb\nhost = %s\nuser = %s\n' "$server_remote" "$SMB_HOST" "$RCLONE_CONFIG_SMB_USER"
      [ -n "${RCLONE_CONFIG_SMB_DOMAIN:-}" ] && printf 'domain = %s\n' "$RCLONE_CONFIG_SMB_DOMAIN"
      [ -n "${RCLONE_CONFIG_SMB_PASS:-}" ] && printf 'pass = %s\n' "$RCLONE_CONFIG_SMB_PASS"
      printf 'use_kerberos = false\n'
      [ -n "${RCLONE_CONFIG_SMB_SPN:-}" ] && printf 'spn = %s\n' "$RCLONE_CONFIG_SMB_SPN"
      [ -n "$root" ] && printf '\n[smb]\ntype = alias\nremote = smb-server:%s\n' "$root"
      printf '\n[cluster]\ntype = local\n'
      if [ -f "$extra_remotes" ]; then
        printf '\n'
        if [ -n "$root" ]; then
          sed 's/^\([[:space:]]*remote[[:space:]]*=[[:space:]]*\)smb:/\1smb-server:/' "$extra_remotes"
        else
          cat "$extra_remotes"
        fi
      fi
    ) > "$conf"
    unset RCLONE_CONFIG_SMB_TYPE RCLONE_CONFIG_SMB_HOST RCLONE_CONFIG_SMB_USER RCLONE_CONFIG_SMB_DOMAIN \
          RCLONE_CONFIG_SMB_PASS RCLONE_CONFIG_SMB_USE_KERBEROS RCLONE_CONFIG_SMB_SPN RCLONE_CONFIG
    # A caller's backend/remote overrides must not replace this session's
    # password or re-enable rclone's broken Kerberos path.
    for inherited in ${!RCLONE_CONFIG_@} ${!RCLONE_SMB_@}; do unset "$inherited"; done
    gui_user="hpclib"
    gui_pass=$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')
    # the cluster's own folders, visible in the image
    for d in /scratch "/scratch/user/$(id -un)" "${SCRATCH:-}"; do
      [ -n "$d" ] && [ -d "$d" ] && binds+=(--bind "$d")
    done
    binds+=(--bind "$run_dir")
    if [ -z "$gui_prog" ]; then
      # rclone's GUI takes its login in the address for the page, and again (login_token) for its API calls
      token=$(printf '%s:%s' "$gui_user" "$gui_pass" | base64 | tr -d '\n' | tr '+/' '-_' | sed 's/=/%3D/g')
      echo "HPCLIB_TRANSFER_GUI port=$port user=$gui_user pass=$gui_pass query=login_token=$token"
      echo "smbshell: rclone's web GUI on 127.0.0.1:$port of $(hostname -s); remotes: smb${root:+ ($root), smb-server}, cluster${extra_names:+, ${extra_names% }}" >&2
      ( img_exec rclone rcd --config "$conf" --rc-web-gui --rc-web-gui-no-open-browser --rc-addr "127.0.0.1:$port" \
          --rc-user "$gui_user" --rc-pass "$gui_pass" --smb-use-kerberos=false --retries 1 --low-level-retries 1 < /dev/null ) &
    else
      # rclone in the image, with the session's remotes and sign-in, for the GUI to call
      need_image
      {
        printf '#!/bin/bash\n'
        printf 'exec'
        printf ' %q' "${image_environment[@]}" "$runtime" exec "${binds[@]}" "${kerberos_binds[@]}" "$SMB_IMAGE" rclone --config "$conf" --smb-use-kerberos=false
        printf ' "$@"\n'
      } > "$run_dir/rclone"
      chmod 700 "$run_dir/rclone"
      run=(bash "$gui_prog")                     # a settings package's NAME.gui.sh isn't executable
      [ "$gui_prog" = "$gui" ] && [ -x "$gui" ] && run=("$gui")
      echo "smbshell: $gui on 127.0.0.1:$port of $(hostname -s); remotes: smb${root:+ ($root), smb-server}, cluster${extra_names:+, ${extra_names% }}" >&2
      ( export HPCLIB_GUI_PORT="$port" HPCLIB_GUI_USER="$gui_user" HPCLIB_GUI_PASS="$gui_pass" \
               HPCLIB_GUI_DIR="$run_dir" RCLONE_CONFIG="$conf" SMBSHELL_RCLONE="$run_dir/rclone"
        exec "${run[@]}" < /dev/null ) &
    fi
    gui_pid=$!
    # the session's parent (sshd, or the shell that ran this): when it is gone the session is, even if no hangup
    # arrived (a dropped connection), so the GUI stops too
    parent=$PPID
    while kill -0 "$gui_pid" 2> /dev/null; do
      if ! kill -0 "$parent" 2> /dev/null; then
        echo "smbshell: the session that started the GUI has ended; stopping it" >&2
        exit 129
      fi
      sleep 5 &
      wait $! 2> /dev/null
    done
    wait "$gui_pid"
    exit $? ;;
  *) die "unknown command $cmd (smbshell --help)" ;;
esac
