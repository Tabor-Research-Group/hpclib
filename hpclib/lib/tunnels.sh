

function _launch_app {
  local default_path="$1"
  shift
  local fallback_name="$1"
  shift

  if [ -f "$default_path" ];
    then
      echo $("$default_path" "$@")
    else
      echo $(open -na "$fallback_name" --args "$@")
  fi
}

# For apps with no CLI flags of their own - just hand the URL/args to
# `open` directly so it fires a normal "open URL" event, rather than
# `--args`, which skips URL interpretation entirely and hands argv
# raw to the app (fine for Chrome's --app=, meaningless for Safari,
# which then treats a bare "http://..." string as a relative file
# path inside its own sandboxed container).
function _launch_app_url {
  local default_path="$1"
  shift
  local fallback_name="$1"
  shift

  if [ -f "$default_path" ];
    then
      echo $("$default_path" "$@")
    else
      echo $(open -a "$fallback_name" "$@")
  fi
}

#CHROME_CODE_DATA_PROFILES="/tmp/chrome-coder-data-dir"
#DEFAULT_CHROME_LAUNCH_PATH="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
function _launch_chrome {
  local browser_mode="$1"
  shift
  local app="$1"
  shift
  if [ -z "$browser_mode" ]; then
    app="--app=$app"
  fi
  echo $(_launch_app "$DEFAULT_CHROME_LAUNCH_PATH" "/Applications/Google Chrome.app" "$app" "$@")
}

function _launch_code_launcher {
  local browser_mode="$1"
  shift
  local app="$1"
  shift
  if [ -z "$browser_mode" ]; then
    app="--app=$app"
  fi
  echo $(_launch_app "$DEFAULT_CODELAUNCHER_LAUNCH_PATH" "CodeLauncher" "$app" "$@")
}

function _launch_chromium {
  local browser_mode="$1"
  shift
  local app="$1"
  shift
  echo $(_launch_app "$DEFAULT_CHROMIUM_LAUNCH_PATH" "Chromium" "--app=$app" "$@")
}

function _launch_safari {
  local browser_mode="$1"
  shift
  echo $(_launch_app_url "$DEFAULT_SAFARI_LAUNCH_PATH" "Safari" "$@")
}

function _launch_firefox {
  local browser_mode="$1"
  shift
  echo $(_launch_app_url "$DEFAULT_FIREFOX_LAUNCH_PATH" "Firefox" "$@")
}

DEFAULT_LAUNCH_BROWSER="Chrome"
function locate_browser_launcher {
  local app="$1"
  if [ -z "$app" ]; then
    app="$DEFAULT_LAUNCH_BROWSER"
  fi
  local located="false"
  local exists=$(declare -f "$app" > /dev/null)

  if [ "$exists" = 1 ]; then
    unset located
    printf "$app"
  fi

  if [ -n "$located" ]; then
    case "$app" in
        "Chrome")
          printf "_launch_chrome"
          ;;
        "CodeLauncher")
          printf "_launch_code_launcher"
          ;;
        "Chromium")
          printf "_launch_chromium"
          ;;
        "Safari")
          printf "_launch_safari"
          ;;
        "Firefox")
          printf "_launch_firefox"
          ;;
    esac
  fi
}

# Polls 127.0.0.1:PORT until something accepts a connection, or gives
# up after RETRIES * WAIT seconds. Used to delay opening the browser
# until the SSH -L forward is actually live and something (first the
# waiting shim, later the real service) is listening on the far end -
# opening the browser any earlier is a guaranteed connection-refused,
# since the forward doesn't exist until ssh finishes connecting.
function _wait_for_port {
  local port="$1"
  local retries="${2:-120}"
  local wait_time="${3:-1}"
  local i

  for ((i = 0; i < retries; i++)); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      exec 3>&- 3<&-
      return 0
    fi
    sleep "$wait_time"
  done
  return 1
}

# Tunnel names are directory names, never paths. Reject traversal and
# separators before using a name in either a lookup or an installation.
function _hpclib_valid_tunnel_name {
  case "$1" in
    ''|[!a-zA-Z0-9]*|*[!a-zA-Z0-9_.-]*) return 1 ;;
  esac
  return 0
}

# Print the first tunnel directory containing an sbatch script.
# HPCLIB_TUNNEL_PATH is searched from left to right.
function resolve_tunnel {
  local name="$1" root
  local roots=()
  _hpclib_valid_tunnel_name "$name" || return 2
  IFS=: read -r -a roots <<< "$HPCLIB_TUNNEL_PATH"
  for root in "${roots[@]}"; do
    if [ -n "$root" ] && [ -f "$root/$name/sbatch_script.sh" ]; then
      printf '%s\n' "$root/$name"
      return 0
    fi
  done
  return 1
}

# Resolve a tunnel-specific file through the same path, then fall back
# to the common file shipped in hpclib/tunnels. The second argument is
# optional inside start_tunnel.sh, where TUNNEL_NAME is already set.
function resolve_tunnel_file {
  local file="$1" name="${2:-$TUNNEL_NAME}" root
  local roots=()
  _hpclib_valid_tunnel_name "$name" || return 2
  case "$file" in
    ''|.|..|*/*) return 2 ;;
  esac
  IFS=: read -r -a roots <<< "$HPCLIB_TUNNEL_PATH"
  for root in "${roots[@]}"; do
    if [ -n "$root" ] && [ -f "$root/$name/$file" ]; then
      printf '%s\n' "$root/$name/$file"
      return 0
    fi
  done
  if [ -f "$HPCTUNNELS_DIR/$file" ]; then
    printf '%s\n' "$HPCTUNNELS_DIR/$file"
    return 0
  fi
  return 1
}
export -f _hpclib_valid_tunnel_name resolve_tunnel resolve_tunnel_file

# Install one downloaded/local tunnel folder. --target names the parent
# directory into which the tunnel's own folder is placed.
function install_tunnel {
  local source='' target="$HPCLIB_TUNNEL_INSTALL_LOCATION" name destination staging
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --target)
        if [ "$#" -lt 2 ] || [ -z "$2" ]; then
          echo 'install_tunnel: --target requires a directory' >&2
          return 2
        fi
        target="$2"; shift 2 ;;
      --target=*)
        target="${1#--target=}"; shift ;;
      --)
        shift
        if [ "$#" -ne 1 ] || [ -n "$source" ]; then
          echo 'usage: install_tunnel [--target DIRECTORY] TUNNEL_FOLDER' >&2
          return 2
        fi
        source="$1"; shift ;;
      -*)
        echo "install_tunnel: unknown option: $1" >&2
        return 2 ;;
      *)
        if [ -n "$source" ]; then
          echo 'usage: install_tunnel [--target DIRECTORY] TUNNEL_FOLDER' >&2
          return 2
        fi
        source="$1"; shift ;;
    esac
  done
  if [ -z "$source" ] || [ -z "$target" ] || [ ! -d "$source" ]; then
    echo 'usage: install_tunnel [--target DIRECTORY] TUNNEL_FOLDER' >&2
    return 2
  fi
  source="$(cd -P "$source" && pwd)" || return 1
  name="${source##*/}"
  if ! _hpclib_valid_tunnel_name "$name" || [ ! -f "$source/sbatch_script.sh" ]; then
    echo "install_tunnel: $source is not a valid tunnel folder" >&2
    return 2
  fi
  mkdir -p "$target" || return 1
  target="$(cd -P "$target" && pwd)" || return 1
  destination="$target/$name"
  if [ -e "$destination" ] || [ -L "$destination" ]; then
    echo "install_tunnel: $destination already exists" >&2
    return 1
  fi
  staging="$(mktemp -d "$target/.$name.install.XXXXXX")" || return 1
  if ! cp -R "$source/." "$staging/"; then
    rm -rf "$staging"
    return 1
  fi
  if ! mv "$staging" "$destination"; then
    rm -rf "$staging"
    return 1
  fi
  if [ -f "$destination/install.sh" ]; then
    if ! (cd "$destination" && HPCLIB_TUNNEL_DIR="$destination" bash ./install.sh) >&2; then
      echo "install_tunnel: install.sh failed for $name" >&2
      rm -rf "$destination"
      return 1
    fi
  fi
  printf '%s\n' "$destination"
}

################################################################################
##
##  Installing hpclib itself on a remote (SLURM) system
##

# Where install_hpclib puts hpclib and where launch_tunnel looks for it,
# relative to the remote home directory unless absolute.
HPCLIB_REMOTE_INSTALL_LOCATION="${HPCLIB_REMOTE_INSTALL_LOCATION:-hpclib}"

# Print the HPCLIB_VERSION assigned in an hpclib.sh, without sourcing it.
function _hpclib_read_version {
  sed -n 's/^[[:space:]]*\(export[[:space:]]\{1,\}\)\{0,1\}HPCLIB_VERSION=["'\'']\{0,1\}\([0-9A-Za-z.]*\).*/\2/p' "$1" 2>/dev/null | head -n 1
}

# Print -1, 0 or 1 as dotted version $1 is older than, equal to, or newer
# than $2. Numeric fields compare as numbers (1.10 > 1.9) and missing
# fields count as 0 (1.2 = 1.2.0); other fields compare as strings.
function _hpclib_version_compare {
  local a=() b=() i n x y
  IFS=. read -r -a a <<< "$1"
  IFS=. read -r -a b <<< "$2"
  n=${#a[@]}
  if [ "${#b[@]}" -gt "$n" ]; then n=${#b[@]}; fi
  for ((i = 0; i < n; i++)); do
    x="${a[$i]:-0}"; y="${b[$i]:-0}"
    if [[ "$x" =~ ^[0-9]+$ ]] && [[ "$y" =~ ^[0-9]+$ ]]; then
      x=$((10#$x)); y=$((10#$y))
      if [ "$x" -lt "$y" ]; then echo -1; return; fi
      if [ "$x" -gt "$y" ]; then echo 1; return; fi
    elif [ "$x" != "$y" ]; then
      if [[ "$x" < "$y" ]]; then echo -1; else echo 1; fi
      return
    fi
  done
  echo 0
}

# Strip a leading ~/ so remote paths can be passed quoted; the remote
# shell starts in the home directory, so relative paths land there.
function _hpclib_remote_path {
  case "$1" in
    '~') printf '.\n' ;;
    '~/'*) printf '%s\n' "${1#\~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

# Runs ON THE REMOTE HOST (shipped there by install_hpclib with
# `declare -f`). In install mode the package tarball follows this script
# on stdin. The version check is done here, at install time, so two
# installs racing each other can't downgrade one another.
function _hpclib_remote_install {
  local mode="$1" target="$2" new_version="$3" force="$4"
  local installed='' state cmp='' parent name staging staged_version
  case "$target" in
    /*) ;;
    *) target="$HOME/$target" ;;
  esac
  if [ -f "$target/hpclib.sh" ]; then
    state=installed
    installed=$(_hpclib_read_version "$target/hpclib.sh")
  elif [ -e "$target" ] || [ -L "$target" ]; then
    state=foreign
  else
    state=missing
  fi
  if [ -n "$installed" ]; then
    cmp=$(_hpclib_version_compare "$installed" "$new_version")
  fi

  local skip=''
  if [ "$force" != true ]; then
    if [ "$state" = foreign ]; then
      echo "install_hpclib: $target exists and is not an hpclib install; use --force to replace it (it is kept as $target.previous)" >&2
      [ "$mode" = install ] && cat > /dev/null
      return 1
    elif [ "$cmp" = 1 ]; then
      skip="hpclib $installed at $target is newer than $new_version; not installing (use --force to downgrade)"
    elif [ "$cmp" = 0 ]; then
      skip="hpclib $installed at $target is up to date"
    fi
  fi
  if [ -n "$skip" ]; then
    echo "$skip"
    [ "$mode" = install ] && cat > /dev/null
    return 0
  fi

  local was="nothing"
  case "$state" in
    installed) was="hpclib ${installed:-(unversioned)}" ;;
    foreign) was="a non-hpclib $target" ;;
  esac
  if [ "$mode" = check ]; then
    echo "would install hpclib $new_version at $target (replacing $was)"
    return 0
  fi

  parent=$(dirname "$target"); name=$(basename "$target")
  mkdir -p "$parent" || { cat > /dev/null; return 1; }
  staging=$(mktemp -d "$parent/.$name.install.XXXXXX") || { cat > /dev/null; return 1; }
  if ! tar -xzf - -C "$staging"; then
    echo "install_hpclib: failed to unpack hpclib on the remote host; $target is unchanged" >&2
    rm -rf "$staging"
    return 1
  fi
  staged_version=$(_hpclib_read_version "$staging/hpclib.sh")
  if [ "$staged_version" != "$new_version" ]; then
    echo "install_hpclib: unpacked version '$staged_version' does not match $new_version; $target is unchanged" >&2
    rm -rf "$staging"
    return 1
  fi
  chmod 755 "$staging"  # mktemp -d makes it 700
  if [ "$state" != missing ]; then
    rm -rf "$target.previous"
    mv "$target" "$target.previous" || { rm -rf "$staging"; return 1; }
  fi
  if ! mv "$staging" "$target"; then
    [ "$state" != missing ] && mv "$target.previous" "$target"
    rm -rf "$staging"
    return 1
  fi
  echo "installed hpclib $new_version at $target (replaced $was)"
  if [ "$state" != missing ]; then
    echo "previous install kept at $target.previous"
  fi
}

# Tar up an hpclib package directory to stdout, without caches or
# macOS metadata.
function _hpclib_pack {
  local tar_flags=()
  if tar --version 2>/dev/null | grep -q bsdtar; then
    tar_flags=(--no-mac-metadata --no-xattrs)
  fi
  (cd "$1" && COPYFILE_DISABLE=1 tar "${tar_flags[@]}" \
    --exclude '__pycache__' --exclude '*.pyc' --exclude '.DS_Store' -czf - .)
}

# Install (or upgrade) this copy of hpclib on a remote host over the
# persistent pssh connection. Takes the same login arguments as
# pssh/psftp ([ssh options] [user@]host). Installs only if the remote copy
# is missing or older than this one, judged by HPCLIB_VERSION in hpclib.sh.
function install_hpclib {
  local usage='usage: install_hpclib [--target DIRECTORY] [--force] [--check] [ssh options] [user@]host'
  local target="$HPCLIB_REMOTE_INSTALL_LOCATION" force=false mode=install
  local login_args=() hosts=() source version script remote_command
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --target)
        if [ "$#" -lt 2 ] || [ -z "$2" ]; then
          echo 'install_hpclib: --target requires a directory' >&2
          return 2
        fi
        target="$2"; shift 2 ;;
      --target=*)
        target="${1#--target=}"; shift ;;
      --force)
        force=true; shift ;;
      --check)
        mode=check; shift ;;
      -h|--help)
        echo "$usage"; return 0 ;;
      *)
        login_args+=("$1"); shift ;;
    esac
  done
  hosts=($(mcargs "$SSH_FLAGS" "$SSH_LONG_FLAGS" "${login_args[@]}"))
  if [ "${#hosts[@]}" -ne 1 ]; then
    echo "$usage" >&2
    return 2
  fi
  target=$(_hpclib_remote_path "$target")
  case "$target" in
    ''|.|..|/|./|../)
      echo "install_hpclib: refusing to install into '$target'" >&2
      return 2 ;;
  esac

  source="$(cd -P "$HPCLIB_DIR" 2>/dev/null && pwd)"
  if [ -z "$source" ] || [ ! -f "$source/hpclib.sh" ]; then
    echo "install_hpclib: HPCLIB_DIR ($HPCLIB_DIR) is not an hpclib directory" >&2
    return 1
  fi
  version=$(_hpclib_read_version "$source/hpclib.sh")
  if [ -z "$version" ]; then
    echo "install_hpclib: no HPCLIB_VERSION found in $source/hpclib.sh" >&2
    return 1
  fi

  # The script travels on stdin ahead of the tarball: `bash -s` reads a
  # pipe one command at a time, so the tar inside the function gets the
  # rest of the stream. This also keeps the remote command line to plain
  # words, which works whatever the remote login shell is.
  script="$(declare -f _hpclib_read_version _hpclib_version_compare _hpclib_remote_install)
_hpclib_remote_install \"\$@\"; exit \$?"
  printf -v remote_command '%q ' bash -s -- "$mode" "$target" "$version" "$force"
  if [ "$mode" = check ]; then
    printf '%s\n' "$script" | pssh "${login_args[@]}" "$remote_command"
  else
    { printf '%s\n' "$script"; _hpclib_pack "$source"; } | pssh "${login_args[@]}" "$remote_command"
  fi
}

################################################################################
##
##  Clearing up after tunnels on a login node
##
##  start_tunnel.sh records itself in $HPCSESSIONS_DIR/ports/HOST-PORT as
##  "PID [JOB]". A later tunnel on the same port, or stop_tunnel, uses the
##  record to stop what an earlier tunnel left running: the tunnel script,
##  its SLURM job, the ssh forward to the compute node, the waiting page.
##

function _hpclib_sessions_root {
  printf '%s\n' "${HPCSESSIONS_DIR:-${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/sessions}"
}

function _hpclib_port_file {  # _hpclib_port_file PORT [HOST]
  printf '%s/ports/%s-%s\n' "$(_hpclib_sessions_root)" "${2:-$(hostname -s)}" "$1"
}

function _hpclib_port_free {  # _hpclib_port_free PORT: can 127.0.0.1:PORT be bound here?
  python3 -c '
import socket, sys
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind(("127.0.0.1", int(sys.argv[1])))
except OSError:
    sys.exit(1)' "$1" 2>/dev/null
}

function _hpclib_record_port {  # _hpclib_record_port PORT PID [JOB]
  local file
  file=$(_hpclib_port_file "$1")
  mkdir -p "$(dirname "$file")"
  printf '%s %s\n' "$2" "${3:-}" > "$file"
}

function _hpclib_forget_port {  # _hpclib_forget_port PORT PID: remove the record if it is still PID's
  local file pid job
  file=$(_hpclib_port_file "$1")
  [ -f "$file" ] || return 0
  read -r pid job < "$file"
  if [ "$pid" = "$2" ]; then rm -f "$file"; fi
}

# _hpclib_clear_port PORT: stop whatever an earlier tunnel left on PORT on
# this login node, then check the port is free. Only touches your own
# processes, and only ones that look like hpclib tunnel pieces for PORT.
function _hpclib_clear_port {
  local port="$1" me file pid job other tries
  me=$(id -un)
  file=$(_hpclib_port_file "$port")
  if [ -f "$file" ]; then
    read -r pid job < "$file"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && ps -o args= -p "$pid" 2>/dev/null | grep -q start_tunnel; then
      echo "stopping the earlier tunnel on port $port (pid $pid)" >&2
      pkill -TERM -P "$pid" 2>/dev/null || true
      kill -TERM "$pid" 2>/dev/null || true
    fi
    if [ -n "$job" ] && [ -n "$(squeue -h -j "$job" -o %i 2>/dev/null)" ]; then
      echo "cancelling its job $job" >&2
      scancel "$job" 2>/dev/null || true
    fi
    rm -f "$file"
  fi
  # pieces that outlive their tunnel script: the forward to the compute
  # node (see connect_to_job) and the waiting page
  if pkill -TERM -u "$me" -f "^ssh -L 127\.0\.0\.1:$port:" 2>/dev/null; then
    echo "stopped a leftover port forward on $port" >&2
  fi
  if pkill -TERM -u "$me" -f "waiting_shim\.py $port " 2>/dev/null; then
    echo "stopped a leftover waiting page on $port" >&2
  fi
  for other in "$(_hpclib_sessions_root)"/ports/*-"$port"; do
    if [ -f "$other" ] && [ "$other" != "$file" ]; then
      echo "note: a tunnel on port $port was also started on ${other##*/ports/}; clear it there if it is stale" >&2
    fi
  done
  for tries in 1 2 3 4 5 6 7 8 9 10; do
    if _hpclib_port_free "$port"; then return 0; fi
    sleep 0.5
  done
  echo "port $port on $(hostname -s) is in use by something that isn't one of your tunnels:" >&2
  ss -ltnp "sport = :$port" 2>/dev/null | tail -n +2 >&2 || true
  echo "pick another port with -P" >&2
  return 1
}

# Stop a tunnel from your own machine, including anything it left behind
# on the login node, and drop the local forward:
#   stop_tunnel -P PORT [ssh options] [user@]host
function stop_tunnel {
  local usage='usage: stop_tunnel -P PORT [ssh options] [user@]host'
  local port='' login_args=() hosts=() script
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -P) port="$2"; shift 2 ;;
      -P*) port="${1#-P}"; shift ;;
      -h|--help) echo "$usage"; return 0 ;;
      *) login_args+=("$1"); shift ;;
    esac
  done
  hosts=($(mcargs "$SSH_FLAGS" "$SSH_LONG_FLAGS" "${login_args[@]}"))
  case "$port" in
    ''|*[!0-9]*) echo "$usage" >&2; return 2 ;;
  esac
  if [ "${#hosts[@]}" -ne 1 ]; then
    echo "$usage" >&2
    return 2
  fi
  script="$(declare -f _hpclib_sessions_root _hpclib_port_file _hpclib_port_free _hpclib_clear_port)
_hpclib_clear_port \"\$1\" && echo \"port \$1 is free on \$(hostname -s)\""
  printf '%s\n' "$script" | HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" "$(printf '%q ' bash -s -- "$port")"
  # the forward this machine's ssh master holds for the tunnel
  HPCLIB_ECHO_COMMANDS= pssh -O cancel -L "127.0.0.1:$port:127.0.0.1:$port" "${login_args[@]}" 2>/dev/null || true
}

LAUNCH_TUNNEL_DEFAULT_APP="Safari"
LAUNCH_TUNNEL_ARGS="bP:A:"
LAUNCH_TUNNEL_LONG_ARGS="browser-arg:"
LAUNCH_TUNNEL_RSYNC="false"
function launch_tunnel {
  # Keep arguments after -- intact for the tunnel's sbatch script. The
  # older option parser returns a flat string, so do not pass script
  # arguments through it (factory expressions can contain spaces).
  local launch_args=() tunnel_script_args=() has_script_args=false
  while [ "$#" -gt 0 ]; do
    if [ "$1" = "--" ]; then
      has_script_args=true
      shift
      tunnel_script_args=("$@")
      break
    fi
    launch_args+=("$1")
    shift
  done
  set -- "${launch_args[@]}"
  local port=$(mcoptvalue "$LAUNCH_TUNNEL_ARGS" "$LAUNCH_TUNNEL_LONG_ARGS" "P" "$@");
  local app=$(mcoptvalue "$LAUNCH_TUNNEL_ARGS" "$LAUNCH_TUNNEL_LONG_ARGS" "A" "$@");
  local browser_mode=$(mcoptvalue "$LAUNCH_TUNNEL_ARGS" "$LAUNCH_TUNNEL_LONG_ARGS" "b" "$@");
  local browser_extra=$(mclongvalue "$LAUNCH_TUNNEL_LONG_ARGS" "browser-arg" "$@");
  # everything NOT recognized above - address, tunnel name, and any
  # sbatch-bound flags like --mem=/--time=/--env= - passes straight
  # through here, untouched and in order
  local args=$(mcargs "$LAUNCH_TUNNEL_ARGS" "$LAUNCH_TUNNEL_LONG_ARGS" "$@");
  args=($args)
  local address="${args[0]}"
  local tunnel="${args[1]}"
  local remote_args=("${args[@]:2}")   # e.g. --mem=30GB - forwarded to start_tunnel.sh, NOT the browser
  local launcher remote_command
  local remote_hpclib=$(_hpclib_remote_path "$HPCLIB_REMOTE_INSTALL_LOCATION")

  if [ "$has_script_args" = true ]; then
    remote_args+=(-- "${tunnel_script_args[@]}")
  fi

  if [ -z "$address" ]; then
    echo  "launch_tunnel requires address and tunnel name"
    else
      if [ -z "$tunnel" ]; then
        echo  "launch_tunnel requires a tunnel name"
        else
          if [ -z "$port" ]; then
            port=$(random_port)
          fi

          if [ -z "$app" ]; then
            app="$LAUNCH_TUNNEL_DEFAULT_APP"
          fi

          PS1="\u\@$address-$TUNNEL\$ "
          PROMPT_COMMAND="echo -ne \"\033]0;$address-$TUNNEL: \${PWD}\007\""
          echo -ne "\033]0;$address-$TUNNEL\007"

          if [ "$LAUNCH_TUNNEL_RSYNC" = "true" ]; then
            psync -r $HPCLIB_DIR $address:$remote_hpclib/
          fi

          # -A none: no browser, e.g. for the REST tunnel
          if [ "$app" != "none" ]; then
          launcher=$(locate_browser_launcher "$app")
          # Wait for the forward to actually be live before opening the
          # browser, in a background subshell - NOT backgrounding pssh
          # itself, so the foreground/blocking/cleanup behavior below
          # is unchanged.
          (
            if _wait_for_port "$port"; then
              # only genuine browser flags (--browser-arg=...) go to the
              # launcher now - not "whatever was left over"
              $launcher "$browser_mode" http://localhost:$port $browser_extra
            else
              echo "Timed out waiting for tunnel on port $port" >&2
            fi
          ) &
          fi

          printf -v remote_command '%q ' /bin/bash "$remote_hpclib/tunnels/start_tunnel.sh" "$tunnel" -P "$port" "${remote_args[@]}"
          printf '%s\n' "pssh -t -L 127.0.0.1:$port:127.0.0.1:$port $address \"$remote_command\""
          pssh -t -L "127.0.0.1:$port:127.0.0.1:$port" "$address" "$remote_command"
      fi
  fi
}
