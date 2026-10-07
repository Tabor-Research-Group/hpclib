#!/usr/bin/env bash
# smbshell: files to and from an SMB server with rclone, from the data-transfer-tools image, on a cluster's login
# node, or in a SLURM job (submit). From your own machine: smbshell --on [ssh options] user@host COMMAND ...
#
#   smbshell ls [REMOTE] [--json] [rclone flags]    list (--json: rclone lsjson; no REMOTE: SMB_ROOT, or the shares)
#   smbshell get REMOTE LOCAL [rclone flags]        copy from the server (rclone copy)
#   smbshell put LOCAL REMOTE [rclone flags]        copy to the server
#   smbshell sync pull REMOTE LOCAL [rclone flags]  make LOCAL match REMOTE (rclone sync: deletes what REMOTE lacks)
#   smbshell sync push LOCAL REMOTE [rclone flags]  make REMOTE match LOCAL (likewise; try --dry-run first)
#   smbshell submit [sbatch options] get|put|sync ...   the same, as a SLURM job (--time=..., --mem=...)
#   smbshell submit [sbatch options] --manifest FILE.json   one job per entry, as an array: a JSON list of
#                                                   argument lists, e.g. [["get", "proj/raw", "/scratch/.../raw"]]
#   smbshell jobs                                   your transfer jobs in the queue
#   smbshell login | logout                         a Kerberos ticket for the server (kinit / kdestroy)
#   smbshell find-spn                               which name the server has in Kerberos (for SMB_SPN), when
#                                                   cifs/SMB_HOST is "not found in Kerberos database"
#   smbshell save-credentials | forget-credentials  a saved password, for jobs where there is no Kerberos
#   smbshell status [--json]                        the image, and how it would sign in
#   smbshell shell                                  a shell in the image, with the server as rclone's "smb:"
#   smbshell gui --port PORT                        rclone's web GUI (rclone rcd --rc-web-gui) on 127.0.0.1:PORT
#                                                   of this node, signed in for this session only (the console's
#                                                   Rclone page forwards it to your browser)
#   smbshell rclone ARGS...                         rclone itself, likewise
#
# REMOTE is a path under SMB_ROOT (e.g. research/our_group) when that is set, else SHARE/PATH on
# SMB_HOST; /SHARE/PATH and //HOST/SHARE/PATH are absolute. Signing in (SMB_AUTH=auto): a Kerberos ticket from
# `smbshell login`, else the saved password, else it asks (never in a job). Settings: settings.sh here.

set -o pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$here/settings.sh"
runtime=$(command -v singularity || command -v apptainer || true)
logdir="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/sessions/data-transfer"
binds=()

die() { echo "smbshell: $*" >&2; exit 1; }
usage() { sed -n '2,24p' "$here/smbshell.sh" | sed 's/^# \{0,1\}//'; }

need_image() {
  [ -n "$runtime" ] || die "neither singularity nor apptainer is on this node's PATH"
  [ -f "$SMB_IMAGE" ] || die "no image at $SMB_IMAGE; install it (setup_tunnel.sh data-transfer --install, or the console's Install)"
}

# the host's krb5.conf, for kinit and rclone in the image (the image's own may not know the realm)
# rclone's Kerberos library (gokrb5) reads less than MIT's: no include/includedir lines, only true/false for
# dns_canonicalize_hostname, and it looks KDCs up in DNS only when told to (MIT does by default, which is how
# clusters find AD's). It gets a copy that says so (MIT's kinit on the node still reads the original).
krb5_conf="${SMB_KRB5_CONF:-}"
[ -z "$krb5_conf" ] && [ -f /etc/krb5.conf ] && krb5_conf=/etc/krb5.conf
if [ -n "$krb5_conf" ] && [ -r "$krb5_conf" ]; then
  krb5_copy="$(dirname "$SMB_KRB5CCNAME")/krb5.conf"
  mkdir -p -m 700 "$(dirname "$krb5_copy")"
  add_dns=1
  grep -Eq '^[[:space:]]*dns_lookup_kdc[[:space:]]*=' "$krb5_conf" && add_dns=0
  awk -v add_dns="$add_dns" '
    /^[[:space:]]*include(dir)?[[:space:]]/ { next }
    /^[[:space:]]*dns_canonicalize_hostname[[:space:]]*=[[:space:]]*fallback[[:space:]]*$/ { sub(/fallback/, "false") }
    /^[[:space:]]*\[libdefaults\]/ { print; if (add_dns) print "    dns_lookup_kdc = true"; done = 1; next }
    { print }
    END { if (add_dns && !done) { print "[libdefaults]"; print "    dns_lookup_kdc = true" } }
  ' "$krb5_conf" > "$krb5_copy.tmp" && mv -f "$krb5_copy.tmp" "$krb5_copy"
  krb5_conf="$krb5_copy"
  binds+=(--bind "$krb5_conf:/etc/hpclib-krb5.conf:ro")
fi

img() {  # img COMMAND...: run it in the image, with the binds asked for
  need_image
  if [ -n "$krb5_conf" ]; then
    KRB5_CONFIG=/etc/hpclib-krb5.conf "$runtime" exec "${binds[@]}" "$SMB_IMAGE" "$@"
  else
    "$runtime" exec "${binds[@]}" "$SMB_IMAGE" "$@"
  fi
}

img_exec() {  # img_exec COMMAND...: img, replacing this (sub)shell, so its pid is the image runtime's
  need_image
  if [ -n "$krb5_conf" ]; then
    KRB5_CONFIG=/etc/hpclib-krb5.conf exec "$runtime" exec "${binds[@]}" "$SMB_IMAGE" "$@"
  else
    exec "$runtime" exec "${binds[@]}" "$SMB_IMAGE" "$@"
  fi
}

bind_local() {  # bind_local PATH: make a local path (or, if it doesn't exist yet, its parent) visible in the image
  local path dir
  path=$(cd "$(dirname "$1")" 2>/dev/null && printf '%s/%s' "$(pwd -P)" "$(basename "$1")") ||
    die "no directory $(dirname "$1")"
  dir="$path"
  [ -e "$dir" ] || dir=$(dirname "$dir")
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
  mkdir -p -m 700 "$(dirname "$SMB_KRB5CCNAME")"
  if command -v "$tool" > /dev/null 2>&1; then
    KRB5CCNAME="FILE:$SMB_KRB5CCNAME" "$tool" "$@"
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

# -- signing in -------------------------------------------------------------------------------------

obscure() { img rclone obscure -; }   # rclone's reversible encoding of a password, read from stdin

sign_in() {  # the rclone remote "smb:", signed in the way SMB_AUTH says
  [ -n "${SMB_HOST:-}" ] || die "set SMB_HOST (the server; setup_tunnel.sh data-transfer --set SMB_HOST=... --save)"
  export RCLONE_CONFIG_SMB_TYPE=smb RCLONE_CONFIG_SMB_HOST="$SMB_HOST" RCLONE_CONFIG_SMB_USER="$SMB_USER"
  if [ -n "${SMB_DOMAIN:-}" ]; then
    export RCLONE_CONFIG_SMB_DOMAIN="$SMB_DOMAIN"
  elif [[ "$SMB_USER" == *@* ]]; then
    # user@domain, as smbclient --user=me@example.edu takes it: rclone wants them apart (else its domain is WORKGROUP)
    export RCLONE_CONFIG_SMB_USER="${SMB_USER%@*}" RCLONE_CONFIG_SMB_DOMAIN="${SMB_USER#*@}"
  fi
  # rclone needs no config file: the remote is all in the environment
  export RCLONE_CONFIG=/dev/null
  case "$SMB_AUTH" in
    auto|kerberos)
      if [[ "$SMB_HOST" =~ ^[0-9.]+$ || "$SMB_HOST" == *:* ]]; then
        # a Kerberos ticket names the server (cifs/NAME): there is none for an address
        [ "$SMB_AUTH" = kerberos ] &&
          die "Kerberos needs the server's DNS name, not the address $SMB_HOST; set SMB_HOST to its name"
        has_ticket && echo "smbshell: SMB_HOST is an address, so not Kerberos (it needs the server's name);" \
          "signing in with a password" >&2
      elif has_ticket; then
        export RCLONE_CONFIG_SMB_USE_KERBEROS=true KRB5CCNAME="FILE:$SMB_KRB5CCNAME"
        # the server's name in Kerberos, when it isn't cifs/SMB_HOST (an alias, say): smbshell find-spn
        [ -n "${SMB_SPN:-}" ] && export RCLONE_CONFIG_SMB_SPN="$SMB_SPN"
        return
      fi
      [ "$SMB_AUTH" = kerberos ] && die "no Kerberos ticket (or it has expired): run smbshell login"
      ;;
  esac
  if [ -f "$SMB_CREDENTIALS" ]; then
    RCLONE_CONFIG_SMB_PASS=$(sed -n 's/^pass=//p' "$SMB_CREDENTIALS")
    export RCLONE_CONFIG_SMB_PASS
    return
  fi
  if [ -t 0 ] && [ -z "${SMB_NONINTERACTIVE:-}" ]; then
    local pw
    IFS= read -rsp "Password for ${SMB_DOMAIN:+$SMB_DOMAIN\\}$SMB_USER on $SMB_HOST: " pw
    echo >&2
    RCLONE_CONFIG_SMB_PASS=$(printf '%s' "$pw" | obscure) || die "could not encode the password"
    unset pw
    export RCLONE_CONFIG_SMB_PASS
    return
  fi
  die "no way to sign in to $SMB_HOST: run smbshell login (Kerberos) or smbshell save-credentials (a password for jobs)"
}

rclone_out() {  # how rclone reports: a progress bar at a terminal, one line a minute in a job's log
  if [ -t 1 ]; then echo "--progress"; else echo "--stats=1m --stats-one-line -v"; fi
}

# rclone retries a failed connection (10 low-level retries, 3 retries): with a wrong password or name that is
# dozens of refused logons for one command, enough to lock an AD account. So sign in once first, with no
# retries, and stop at a refusal; the transfer itself keeps rclone's retries for a flaky network.
ONCE="--retries 1 --low-level-retries 1"
check_login() {
  local probe="smb:" share out
  share="${REMOTE#smb:}"
  share="${share%%/*}"
  [ -n "$share" ] && probe="smb:$share"
  if ! out=$(img rclone lsf --max-depth 1 $ONCE "$probe" 2>&1 > /dev/null); then
    if printf '%s' "$out" | grep -qiE 'logon|password|authenticat|kerberos|access denied|LOGON_FAILURE'; then
      die "the server refused the sign-in (tried once, so it doesn't add up to a lockout): $(printf '%s\n' "$out" |
        grep -iE 'logon|password|authenticat|kerberos|access denied' | tail -n 1 | sed 's/^.*couldn.t connect SMB: //')"
    fi
  fi
}

# -- commands ---------------------------------------------------------------------------------------

transfer() {  # ls/get/put/sync, signed in (also what a job runs)
  local cmd="$1"
  shift
  case "$cmd" in
    ls)
      # no REMOTE: SMB_ROOT, or with no SMB_ROOT the server's shares
      if [ "$#" -ge 1 ] && [ "${1#-}" = "$1" ]; then remote "$1"; shift; else remote ""; fi
      local how=lsf
      if [ "${1:-}" = --json ]; then how=lsjson; shift; fi
      sign_in
      img rclone "$how" $ONCE "$REMOTE" "$@" ;;
    get)
      [ "$#" -ge 2 ] || die "usage: smbshell get REMOTE LOCAL [rclone flags]"
      remote "$1"; mkdir -p "$2" 2>/dev/null; bind_local "$2"; shift 2
      sign_in
      check_login
      img rclone copy $(rclone_out) "$REMOTE" "$LOCAL" "$@" ;;
    put)
      [ "$#" -ge 2 ] || die "usage: smbshell put LOCAL REMOTE [rclone flags]"
      [ -e "$1" ] || die "no $1"
      bind_local "$1"; remote "$2"; shift 2
      sign_in
      check_login
      img rclone copy $(rclone_out) "$LOCAL" "$REMOTE" "$@" ;;
    sync)
      [ "$#" -ge 3 ] || die "usage: smbshell sync pull REMOTE LOCAL | sync push LOCAL REMOTE [rclone flags]"
      local way="$1"; shift
      case "$way" in
        pull) remote "$1"; mkdir -p "$2" 2>/dev/null; bind_local "$2"; set -- "$REMOTE" "$LOCAL" "${@:3}" ;;
        push) [ -e "$1" ] || die "no $1"; bind_local "$1"; remote "$2"; set -- "$LOCAL" "$REMOTE" "${@:3}" ;;
        *) die "sync pull REMOTE LOCAL, or sync push LOCAL REMOTE" ;;
      esac
      echo "smbshell: sync makes $2 match $1, deleting what $1 doesn't have (--dry-run shows what it would do)" >&2
      sign_in
      check_login
      img rclone sync $(rclone_out) "$@" ;;
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
  if [ "$SMB_AUTH" != password ] && has_ticket; then
    echo "smbshell: the job signs in with your Kerberos ticket; it must outlive the job:" >&2
    krb klist 2>/dev/null | grep -i -m 1 krbtgt | sed 's/^/  /' >&2 || true
  elif [ "$SMB_AUTH" != kerberos ] && [ -f "$SMB_CREDENTIALS" ]; then
    :
  else
    die "a job can't ask for a password: run smbshell login (Kerberos) or smbshell save-credentials first"
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
    case "${1:-}" in get|put|sync) ;; *) die "submit get|put|sync ..., or submit --manifest FILE" ;; esac
    SMBSHELL_DIR="$here" sbatch --parsable --job-name=smb-transfer --output="$logdir/transfer-%j.log" \
      $defaults "${opts[@]}" "$here/sbatch_script.sh" "$@"
  fi
}

status() {
  local where ticket='' principal='' expires='' creds=false image=false kerb_rclone=false
  where=$(kinit_where)
  [ -f "$SMB_IMAGE" ] && image=true
  [ -f "$SMB_CREDENTIALS" ] && creds=true
  if [ -n "$where" ] && has_ticket; then
    ticket=true
    principal=$(krb klist 2>/dev/null | sed -n 's/^Default principal: *//p' | head -n 1)
    expires=$(krb klist 2>/dev/null | grep -i -m 1 krbtgt | awk '{print $3, $4}')
  fi
  if [ "$image" = true ] && [ -n "$runtime" ] && img rclone help backend smb 2>/dev/null | grep -qi kerberos; then
    kerb_rclone=true
  fi
  if [ "${1:-}" = --json ]; then
    python3 - "$where" "$ticket" "$principal" "$expires" "$creds" "$image" "$kerb_rclone" <<'PY'
import json, os, sys
where, ticket, principal, expires, creds, image, kerb = sys.argv[1:]
print("HPCLIB_SMB_STATUS " + json.dumps({
    "host": os.environ.get("SMB_HOST") or None, "root": os.environ.get("SMB_ROOT") or None,
    "user": os.environ.get("SMB_USER"),
    "auth": os.environ.get("SMB_AUTH"), "kinit": where or None, "rclone_kerberos": kerb == "true",
    "ticket": {"principal": principal, "expires": expires} if ticket == "true" else None,
    "credentials": creds == "true", "image": image == "true", "image_path": os.environ.get("SMB_IMAGE")}))
PY
    return
  fi
  echo "server:      ${SMB_HOST:-(not set: SMB_HOST)} as $SMB_USER${SMB_DOMAIN:+ ($SMB_DOMAIN)}"
  echo "folder:      ${SMB_ROOT:-(none: paths are SHARE/PATH)}"
  echo "image:       $SMB_IMAGE ($([ "$image" = true ] && echo present || echo missing))"
  echo "kinit:       ${where:-not found}$([ "$kerb_rclone" = true ] || echo '; this rclone has no Kerberos')"
  echo "ticket:      $([ -n "$ticket" ] && echo "$principal until $expires" || echo none)"
  echo "saved password: $([ "$creds" = true ] && echo "yes ($SMB_CREDENTIALS)" || echo no)"
}

cmd="${1:-}"
[ -n "$cmd" ] && shift
case "$cmd" in
  ''|-h|--help|help) usage ;;
  ls|get|put|sync) transfer "$cmd" "$@" ;;
  submit) submit "$@" ;;
  jobs) squeue -u "$(id -un)" --name=smb-transfer -o "%.12i %.10T %.10M %.12l %R" ;;
  login)
    [ -n "$(kinit_where)" ] || die "no kinit here or in the image; use smbshell save-credentials (or a password each time)"
    # the Kerberos name is the user in the realm (SMB_REALM, else krb5.conf's default): not SMB_USER's @domain,
    # which is the Windows domain (example.edu), not the realm (AUTH.EXAMPLE.EDU)
    krb kinit "${SMB_USER%@*}${SMB_REALM:+@$SMB_REALM}" "$@" || die "kinit failed"
    echo "signed in: $(krb klist 2>/dev/null | sed -n 's/^Default principal: *//p')"
    krb klist 2>/dev/null | grep -i -m 1 krbtgt ;;
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
      if krb kvno "cifs/$name" > /dev/null 2>&1; then
        echo "known:     cifs/$name"
        [ -z "$found" ] && found="cifs/$name"
      else
        echo "not known: cifs/$name"
      fi
    done
    if [ -n "$found" ]; then
      echo "use it:    setup_tunnel.sh data-transfer --set SMB_SPN=$found --save (or the console's SPN setting)"
    else
      echo "none of these is known to the KDC; ask the server's admins for its CIFS service principal name" >&2
      exit 1
    fi ;;
  save-credentials)
    [ -t 0 ] || die "save-credentials asks for the password at a terminal"
    IFS= read -rsp "Password for ${SMB_DOMAIN:+$SMB_DOMAIN\\}$SMB_USER on ${SMB_HOST:-the SMB server} (saved for sync jobs): " pw
    echo >&2
    [ -n "$pw" ] || die "no password given"
    encoded=$(printf '%s' "$pw" | obscure) || die "could not encode the password"
    unset pw
    mkdir -p -m 700 "$(dirname "$SMB_CREDENTIALS")"
    (umask 077; printf 'pass=%s\n' "$encoded" > "$SMB_CREDENTIALS.tmp" && mv -f "$SMB_CREDENTIALS.tmp" "$SMB_CREDENTIALS")
    echo "saved for jobs in $SMB_CREDENTIALS (mode 600; rclone's encoding is reversible, so it is as good as the password)" ;;
  forget-credentials) rm -f "$SMB_CREDENTIALS"; echo "saved password removed" ;;
  status) status "$@" ;;
  shell)
    sign_in
    need_image
    [ -n "$krb5_conf" ] && export KRB5_CONFIG=/etc/hpclib-krb5.conf
    echo "smbshell: the server is rclone's remote smb: here (rclone lsd smb:SHARE, rclone copy smb:SHARE/x .)" >&2
    exec "$runtime" shell "${binds[@]}" "$SMB_IMAGE" ;;
  rclone) sign_in; img rclone "$@" ;;
  gui)
    # rclone's web GUI on this (login) node, for as long as this runs. Its remotes are in a config file in a
    # private directory in memory (/dev/shm or XDG_RUNTIME_DIR), removed when it stops: smb (the server),
    # cluster (this node's files), and any in $HPCTUNNELS_DATA_DIR/settings/data-transfer.d/rclone.conf (which
    # a settings package installs, e.g. an alias for a group's folder: [ours] type = alias remote = smb:SHARE/DIR).
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
    # marked as hpclib's for this port (see _hpclib_port_holders), and the port cleared of what an earlier GUI
    # or tunnel of yours left on it, before asking for a password
    export HPCLIB_TUNNEL_PORT="$port"
    hpclib_lib="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../lib" 2> /dev/null && pwd)/tunnels.sh"
    if [ -f "$hpclib_lib" ]; then
      # shellcheck source=/dev/null
      . "$hpclib_lib"
      _hpclib_clear_port "$port" || die "port $port is busy; pick another"
    fi
    sign_in
    need_image
    run_dir=$(mktemp -d "${XDG_RUNTIME_DIR:-/dev/shm}/hpclib-rclone-gui.XXXXXX" 2>/dev/null ||
              mktemp -d "${TMPDIR:-/tmp}/hpclib-rclone-gui.XXXXXX") || die "no private directory for the session"
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
    conf="$run_dir/rclone.conf"
    extra_remotes="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/settings/data-transfer.d/rclone.conf"
    extra_names=$( [ -f "$extra_remotes" ] && sed -n 's/^\[\(.*\)\][[:space:]]*$/\1/p' "$extra_remotes" | tr '\n' ' ')
    case " $extra_names " in
      *" smb "*|*" cluster "*) die "$extra_remotes may not define smb or cluster, which smbshell makes" ;;
    esac
    (
      umask 077
      printf '[smb]\ntype = smb\nhost = %s\nuser = %s\n' "$SMB_HOST" "$RCLONE_CONFIG_SMB_USER"
      [ -n "${RCLONE_CONFIG_SMB_DOMAIN:-}" ] && printf 'domain = %s\n' "$RCLONE_CONFIG_SMB_DOMAIN"
      [ -n "${RCLONE_CONFIG_SMB_PASS:-}" ] && printf 'pass = %s\n' "$RCLONE_CONFIG_SMB_PASS"
      [ -n "${RCLONE_CONFIG_SMB_USE_KERBEROS:-}" ] && printf 'use_kerberos = true\n'
      [ -n "${RCLONE_CONFIG_SMB_SPN:-}" ] && printf 'spn = %s\n' "$RCLONE_CONFIG_SMB_SPN"
      printf '\n[cluster]\ntype = local\n'
      if [ -f "$extra_remotes" ]; then
        printf '\n'
        cat "$extra_remotes"
      fi
    ) > "$conf"
    unset RCLONE_CONFIG_SMB_TYPE RCLONE_CONFIG_SMB_HOST RCLONE_CONFIG_SMB_USER RCLONE_CONFIG_SMB_DOMAIN \
          RCLONE_CONFIG_SMB_PASS RCLONE_CONFIG_SMB_USE_KERBEROS RCLONE_CONFIG_SMB_SPN RCLONE_CONFIG
    gui_user="hpclib"
    gui_pass=$(head -c 18 /dev/urandom | od -An -tx1 | tr -d ' \n')
    # the cluster's own folders, visible in the image
    for d in /scratch "/scratch/user/$(id -un)" "${SCRATCH:-}"; do
      [ -n "$d" ] && [ -d "$d" ] && binds+=(--bind "$d")
    done
    binds+=(--bind "$run_dir")
    echo "HPCLIB_RCLONE_GUI port=$port user=$gui_user pass=$gui_pass"
    echo "smbshell: rclone's web GUI on 127.0.0.1:$port of $(hostname -s); remotes: smb, cluster${extra_names:+, ${extra_names% }}" >&2
    ( img_exec rclone rcd --config "$conf" --rc-web-gui --rc-web-gui-no-open-browser --rc-addr "127.0.0.1:$port" \
        --rc-user "$gui_user" --rc-pass "$gui_pass" --retries 1 --low-level-retries 1 < /dev/null ) &
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
