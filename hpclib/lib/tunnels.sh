

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

# From your own machine: check or install what a tunnel needs on a cluster, and save its settings there,
# with the cluster's tunnels/setup_tunnel.sh (see it for the options), e.g.
#   tunnel_setup user@grace.hprc.tamu.edu vscode --set VSCODE_CONTAINER=/scratch/user/me/vscode.sif --save --install --check
function tunnel_setup {
  local usage='usage: tunnel_setup [ssh options] [user@]host TUNNEL [--push DIR] [--set NAME=VALUE]... [--save] [--install [--force]] [--check]'
  local login_args=() host='' tunnel='' rest=() remote_hpclib remote_command push='' given=() i
  while [ "$#" -gt 0 ]; do
    if [ -n "$tunnel" ]; then
      rest=("$@")
      break
    fi
    case "$1" in
      -h|--help) echo "$usage"; return 0 ;;
      -?)
        login_args+=("$1")
        if [[ "$SSH_FLAGS" == *"${1#-}:"* ]] && [ "$#" -ge 2 ]; then login_args+=("$2"); shift; fi
        shift ;;
      -*) login_args+=("$1"); shift ;;
      *) if [ -z "$host" ]; then host="$1"; else tunnel="$1"; fi; shift ;;
    esac
  done
  if [ -z "$host" ] || [ -z "$tunnel" ] || ! _hpclib_valid_tunnel_name "$tunnel"; then
    echo "$usage" >&2
    return 2
  fi
  # --push DIR: send DIR (tunnel/ and settings.d/, as the console stages a packaged app or settings) along
  given=("${rest[@]}") rest=()
  for ((i = 0; i < ${#given[@]}; i++)); do
    if [ "${given[$i]}" = --push ]; then
      push="${given[$((i + 1))]:-}"
      i=$((i + 1))
    else
      rest+=("${given[$i]}")
    fi
  done
  if [ -n "$push" ] && [ ! -d "$push" ]; then
    echo "tunnel_setup: --push needs a directory, not '$push'" >&2
    return 2
  fi
  remote_hpclib=$(_hpclib_remote_path "$HPCLIB_REMOTE_INSTALL_LOCATION")
  if [ -n "$push" ]; then
    remote_command=$(_hpclib_remote_script_cmd /bin/bash "$remote_hpclib/tunnels/setup_tunnel.sh" "$tunnel" --receive "${rest[@]}")
    # plain files only (no owners, no links followed), as setup_tunnel.sh --receive expects. macOS's tar
    # (bsdtar) would add extended attributes (com.apple.provenance, ...) the cluster's GNU tar warns about.
    local tar_opts=()
    if tar --version 2> /dev/null | grep -qi bsdtar; then
      tar_opts=(--no-xattrs --no-acls)
      [ "$(uname -s)" = Darwin ] && tar_opts+=(--no-mac-metadata)
    fi
    (cd "$push" && COPYFILE_DISABLE=1 tar -c -f - "${tar_opts[@]}" --exclude='._*' .) |
      HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" "$host" "$remote_command"
  else
    remote_command=$(_hpclib_remote_script_cmd /bin/bash "$remote_hpclib/tunnels/setup_tunnel.sh" "$tunnel" "${rest[@]}")
    HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" "$host" "$remote_command"
  fi
}

# SMB transfers with rclone from the data-transfer-tools image (tunnels/data-transfer/smbshell.sh; smbshell --help).
# On a cluster it runs there; from your own machine, --on runs it on a cluster's login node over ssh, with a
# terminal, so it can ask for a password:
#   smbshell --on [ssh options] user@host get proj/raw /scratch/user/me/raw
function smbshell {
  local login_args=() host='' remote_hpclib
  if [ "${1:-}" != --on ]; then
    bash "$HPCLIB_DIR/tunnels/data-transfer/smbshell.sh" "$@"
    return
  fi
  shift
  while [ "$#" -gt 0 ] && [ -z "$host" ]; do
    case "$1" in
      -?)
        login_args+=("$1")
        if [[ "$SSH_FLAGS" == *"${1#-}:"* ]] && [ "$#" -ge 2 ]; then login_args+=("$2"); shift; fi
        shift ;;
      -*) login_args+=("$1"); shift ;;
      *) host="$1"; shift ;;
    esac
  done
  if [ -z "$host" ]; then
    echo 'usage: smbshell --on [ssh options] [user@]host COMMAND ...' >&2
    return 2
  fi
  remote_hpclib=$(_hpclib_remote_path "$HPCLIB_REMOTE_INSTALL_LOCATION")
  HPCLIB_ECHO_COMMANDS= pssh -t "${login_args[@]}" "$host" \
    "$(_hpclib_remote_script_cmd /bin/bash "$remote_hpclib/tunnels/data-transfer/smbshell.sh" "$@")"
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

# Strip a leading ~/ so remote paths can be passed quoted. A relative
# remote path means one in the remote home directory; commands use
# _hpclib_remote_home_word for it, since the remote shell need not start
# there (a .bashrc may cd to scratch, and is left to do so).
function _hpclib_remote_path {
  case "$1" in
    '~') printf '.\n' ;;
    '~/'*) printf '%s\n' "${1#\~/}" ;;
    *) printf '%s\n' "$1" ;;
  esac
}

# A path as one word of a remote command line: absolute paths quoted as
# they are, relative ones anchored at the remote "$HOME", which the remote
# shell expands. Commands still run in whatever directory the user's login
# setup leaves them in.
function _hpclib_remote_home_word {  # _hpclib_remote_home_word PATH
  case "$1" in
    /*) printf '%q\n' "$1" ;;
    .) printf '"$HOME"\n' ;;
    *) printf '"$HOME"/%q\n' "$1" ;;
  esac
}

# A remote command line: PROGRAM SCRIPT ARGS..., with SCRIPT (a path in the
# hpclib install) anchored at the remote home and the rest quoted.
function _hpclib_remote_script_cmd {  # _hpclib_remote_script_cmd PROGRAM SCRIPT [ARG...]
  local program="$1" script="$2" quoted=''
  shift 2
  [ "$#" -gt 0 ] && printf -v quoted ' %q' "$@"
  printf '%q %s%s\n' "$program" "$(_hpclib_remote_home_word "$script")" "$quoted"
}

# A remote command line running hpclib's SCRIPT with ARGS through the
# cluster's Python launcher (written by setup_agents; see _hpclib_remote_python).
function _hpclib_remote_python_cmd {  # _hpclib_remote_python_cmd SCRIPT [ARG...]
  local script="$1" quoted=''
  shift
  [ "$#" -gt 0 ] && printf -v quoted ' %q' "$@"
  printf '"${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}"/rest/python %s%s\n' \
    "$(_hpclib_remote_home_word "$script")" "$quoted"
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

  # One install at a time. mkdir is atomic, NFS included; the directory
  # records who holds it so a stale lock can be told from a live one.
  local lock="$parent/.$name.install-lock" holder holder_host holder_pid
  if ! mkdir "$lock" 2>/dev/null; then
    holder=$(cat "$lock/owner" 2>/dev/null)   # "host pid start-time"
    holder_host=${holder%% *}; holder_pid=$(echo "$holder" | cut -d' ' -f2)
    if [ "$holder_host" = "$(hostname)" ] && [ -n "$holder_pid" ] && ! kill -0 "$holder_pid" 2>/dev/null \
        && rm -rf "$lock" && mkdir "$lock" 2>/dev/null; then
      echo "install_hpclib: cleared a stale lock left by pid $holder_pid"
    else
      echo "install_hpclib: another install into $target is running (${holder:-holder unknown});" \
        "if it is stuck, kill it and remove $lock" >&2
      cat > /dev/null
      return 1
    fi
  fi
  echo "$(hostname) $$ $(date '+%Y-%m-%dT%H:%M:%S')" > "$lock/owner"
  # Clean up on every way out, including the ssh connection dropping. The
  # trap runs after this function returns, so it reads globals.
  _hpclib_install_lock=$lock _hpclib_install_staging=''
  trap 'rm -rf "$_hpclib_install_staging" "$_hpclib_install_lock"' EXIT
  trap 'exit 130' HUP INT TERM PIPE

  # Earlier interrupted installs and old backups go into a trash directory
  # that is deleted in the background: a delete that hangs on a slow
  # filesystem must not hold up the install (or this ssh session).
  local trash='' leftover
  _hpclib_install_trash() {
    [ -n "$trash" ] || trash=$(mktemp -d "$parent/.$name.trash.XXXXXX") || return 1
    mv "$1" "$trash/" 2>/dev/null
  }
  for leftover in "$parent/.$name.install."??????; do
    [ -d "$leftover" ] || continue
    echo "install_hpclib: removing the leftovers of an interrupted install ($leftover)"
    _hpclib_install_trash "$leftover"
  done

  staging=$(mktemp -d "$parent/.$name.install.XXXXXX") || { cat > /dev/null; return 1; }
  _hpclib_install_staging=$staging
  echo "unpacking hpclib $new_version into $parent"
  if ! tar -xzf - -C "$staging"; then
    echo "install_hpclib: failed to unpack hpclib on the remote host; $target is unchanged" >&2
    return 1
  fi
  staged_version=$(_hpclib_read_version "$staging/hpclib.sh")
  if [ "$staged_version" != "$new_version" ]; then
    echo "install_hpclib: unpacked version '$staged_version' does not match $new_version; $target is unchanged" >&2
    return 1
  fi
  chmod 755 "$staging"  # mktemp -d makes it 700
  if [ "$state" != missing ]; then
    if [ -e "$target.previous" ] || [ -L "$target.previous" ]; then
      _hpclib_install_trash "$target.previous" || {
        echo "install_hpclib: could not move the old $target.previous aside; $target is unchanged" >&2
        return 1
      }
    fi
    mv "$target" "$target.previous" || return 1
  fi
  if ! mv "$staging" "$target"; then
    [ "$state" != missing ] && mv "$target.previous" "$target"
    return 1
  fi
  _hpclib_install_staging=''
  echo "installed hpclib $new_version at $target (replaced $was)"
  if [ "$state" != missing ]; then
    echo "previous install kept at $target.previous"
  fi
  for leftover in "$parent/.$name.trash."??????; do
    [ -d "$leftover" ] || continue
    # detached, so it outlives this session; it may take a while on a slow filesystem
    if command -v setsid > /dev/null; then
      setsid nohup rm -rf "$leftover" > /dev/null 2>&1 < /dev/null &
    else
      nohup rm -rf "$leftover" > /dev/null 2>&1 < /dev/null &
    fi
    echo "deleting $leftover in the background"
  done
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
##  Setting a cluster up for agents (LLM clients) on the REST server
##

# Runs ON THE REMOTE HOST: choose the Python that hpclib's servers run with
# (HPCLIB_MIN_PYTHON or newer) and record it as the launcher
# ~/.local/tunnels/rest/python, which the REST tunnel and setup_agents run
# everything through. Arguments: REBUILD(yes|no) SPEC, where SPEC is "" (find
# one: python3 or pythonX.Y on PATH, then the newest suitable Python module,
# loading what Lmod's spider says it needs first), "modules:M1 M2 ..." or
# "path:COMMAND". An existing launcher that still works is kept unless
# REBUILD or SPEC says otherwise. Prints a summary line "python|VERSION|HOW".
function _hpclib_remote_python {
  local rebuild="$1" spec="$2" min="${3:-3.9}"
  local data="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/rest"
  local launcher="$data/python" kind='' found='' mods='' version='' c m prereq tried=0
  local check="import sys; sys.exit(sys.version_info < tuple(int(x) for x in '$min'.split('.')))"
  _hp_ok() { "$@" -c "$check" > /dev/null 2>&1; }
  _hp_version() { "$@" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2> /dev/null; }
  _hp_init_modules() {
    type module > /dev/null 2>&1 && return 0
    local f
    for f in /etc/profile.d/lmod.sh /etc/profile.d/modules.sh /usr/share/lmod/lmod/init/bash \
        /usr/share/Modules/init/bash; do
      if [ -f "$f" ]; then
        . "$f" > /dev/null 2>&1
        type module > /dev/null 2>&1 && return 0
      fi
    done
    return 1
  }
  # in a subshell, so the caller's environment is untouched: load the modules, print python3 if new enough
  _hp_try_modules() {
    (
      module load "$@" > /dev/null 2>&1 || exit 1
      local p
      p=$(command -v python3) || exit 1
      _hp_ok "$p" && printf '%s\n' "$p"
    )
  }
  _hp_prereqs() {  # the first set of modules Lmod's spider says must be loaded before $1
    module spider "$1" 2>&1 | awk '/You will need to load all module\(s\) on any one of the lines below/ {f = 1; next}
      f && NF {sub(/^[ \t]+/, ""); sub(/[ \t]+$/, ""); print; exit}'
  }

  mkdir -p "$data" || return 1
  if [ -z "$spec" ] && [ "$rebuild" != yes ] && [ -x "$launcher" ] && _hp_ok "$launcher"; then
    printf 'python|%s|kept the existing %s\n' "$(_hp_version "$launcher")" "$launcher"
    return 0
  fi

  case "$spec" in
    path:*)
      kind=path found="${spec#path:}"
      _hp_ok "$found" || { echo "setup_agents: $found is not Python $min or newer" >&2; return 1; } ;;
    modules:*)
      kind=modules mods="${spec#modules:}"
      _hp_init_modules || { echo "setup_agents: no module command on the cluster" >&2; return 1; }
      found=$(_hp_try_modules $mods) ||
        { echo "setup_agents: loading $mods gives no python3 of version $min or newer" >&2; return 1; } ;;
    '')
      for c in ${HPCLIB_PYTHON_NAMES:-python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9}; do
        c=$(command -v "$c" 2> /dev/null) || continue
        if _hp_ok "$c"; then kind=path found="$c"; break; fi
      done
      if [ -z "$found" ] && _hp_init_modules; then
        # every module named like a Python (or a conda distribution), newest first; spider sees
        # modules that a hierarchy hides from avail
        local candidates
        candidates=$({ module -t spider 2>&1; module -t avail 2>&1; } | sed 's/([^)]*)//g; s/[[:space:]]*$//' |
          grep -E '^([Pp]ython3?|[Aa]naconda3?|[Mm]iniconda3?|[Mm]iniforge3?)/[0-9]' |
          grep -Ev '^[Pp]ython3?/(2\.|3\.[0-8]([^0-9]|$))' | sort -u |
          awk -F/ '{print (tolower($1) ~ /^python/ ? 0 : 1) "\t" $0}' | sort -t"$(printf '\t')" -k1,1 -k2,2Vr |
          cut -f2)
        for m in $candidates; do
          [ "$tried" -lt 8 ] || break
          tried=$((tried + 1))
          if c=$(_hp_try_modules "$m"); then
            kind=modules mods="$m" found="$c"; break
          fi
          prereq=$(_hp_prereqs "$m")
          if [ -n "$prereq" ] && c=$(_hp_try_modules $prereq "$m"); then
            kind=modules mods="$prereq $m" found="$c"; break
          fi
        done
      fi
      if [ -z "$found" ]; then
        echo "setup_agents: found no Python $min or newer, on PATH or as a module; install one (e.g. with" >&2
        echo "  conda or uv) and rerun with --remote-python /path/to/python3, or name its modules with --python-module" >&2
        return 1
      fi ;;
    *) echo "setup_agents: unknown Python spec '$spec'" >&2; return 1 ;;
  esac

  version=$(if [ "$kind" = modules ]; then _hp_init_modules; module load $mods > /dev/null 2>&1; fi; _hp_version "$found")
  local tmp="$launcher.tmp.$$"
  {
    printf '#!/bin/bash\n'
    printf '# Written by setup_agents (hpclib): the Python (%s) that hpclib runs its servers with on this\n' "$version"
    printf '# cluster. Rerun setup_agents with --rebuild, --python-module or --remote-python to change it.\n'
    if [ "$kind" = modules ]; then
      printf '# The environment from before these modules are loaded is saved for the server to give the\n'
      printf "# jobs and commands it starts, so they don't inherit this Python's modules.\n"
      printf 'if [ -z "${HPCLIB_BASE_ENV:-}" ]; then\n'
      printf '  HPCLIB_BASE_ENV=$(env -0 2> /dev/null | base64 | tr -d "\\n") && export HPCLIB_BASE_ENV\n'
      printf 'fi\n'
      printf 'if ! type module > /dev/null 2>&1; then\n'
      printf '  for f in /etc/profile.d/lmod.sh /etc/profile.d/modules.sh /usr/share/lmod/lmod/init/bash /usr/share/Modules/init/bash; do\n'
      printf '    if [ -f "$f" ]; then . "$f" > /dev/null 2>&1; type module > /dev/null 2>&1 && break; fi\n'
      printf '  done\n'
      printf 'fi\n'
      printf 'module load %s > /dev/null 2>&1 || {\n' "$mods"
      printf '  echo "hpclib: could not load %s for Python; rerun setup_agents --rebuild to choose again" >&2\n' "$mods"
      printf '  exit 1\n'
      printf '}\n'
      printf 'exec python3 "$@"\n'
    else
      printf 'exec %q "$@"\n' "$found"
    fi
  } > "$tmp" && chmod 700 "$tmp" && mv "$tmp" "$launcher" || { rm -f "$tmp"; return 1; }
  printf '{"version": "%s", "kind": "%s", "python": "%s", "modules": "%s"}\n' "$version" "$kind" "$found" "$mods" \
    > "$data/python.json"
  if [ "$kind" = modules ]; then
    printf 'python|%s|module %s\n' "$version" "$mods"
  else
    printf 'python|%s|%s\n' "$version" "$found"
  fi
}

# Runs ON THE REMOTE HOST: copy bundled templates and write the REST
# server's config. Arguments: HPCLIB_DIR REBUILD(yes|no) SANDBOX(yes|no)
# TEMPLATES(comma list) BINDS(comma list) WORK_DIR... ; stdin is a base
# config (JSON), or empty.
function _hpclib_remote_setup_agents {
  local hpclib="$1" rebuild="$2" sandbox="$3" templates="$4" binds="$5"
  shift 5
  case "$hpclib" in /*) ;; *) hpclib="$HOME/$hpclib" ;; esac   # don't depend on where the shell started
  local data="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/rest"
  local stamp t d base status=0 python="$data/python"
  local args=(--init-config)
  [ -x "$python" ] || python=python3   # _hpclib_remote_python normally made it first
  stamp=$(date +%Y%m%dT%H%M%S)
  base=$(mktemp) || return 1
  cat > "$base"
  mkdir -p "$data/templates" || { rm -f "$base"; return 1; }
  for d in "$@"; do
    mkdir -p "$d" || { echo "setup_agents: could not create $d" >&2; rm -f "$base"; return 1; }
  done
  local IFS=,
  for t in $templates; do
    [ -n "$t" ] || continue
    if [ ! -d "$hpclib/tunnels/rest/templates/$t" ]; then
      echo "setup_agents: hpclib has no template '$t' (see $hpclib/tunnels/rest/templates)" >&2
      status=1
      continue
    fi
    if [ -e "$data/templates/$t" ]; then
      if [ "$rebuild" != yes ]; then
        echo "kept the existing $t template"
        continue
      fi
      # replaced copies go in a hidden directory, which the server doesn't load
      mkdir -p "$data/templates/.replaced"
      mv "$data/templates/$t" "$data/templates/.replaced/$t-$stamp" || { status=1; continue; }
      cp -R "$hpclib/tunnels/rest/templates/$t" "$data/templates/" || { status=1; continue; }
      echo "replaced the $t template (the old one is in $data/templates/.replaced/$t-$stamp)"
    else
      cp -R "$hpclib/tunnels/rest/templates/$t" "$data/templates/" || { status=1; continue; }
      echo "installed the $t template"
    fi
  done
  [ -s "$base" ] && args+=(--config-base "$base")
  [ "$rebuild" = yes ] && args+=(--rebuild)
  [ "$sandbox" = no ] && args+=(--no-sandbox)
  for d in $binds; do
    [ -n "$d" ] && args+=(--sandbox-bind "$d")
  done
  unset IFS
  "$python" "$hpclib/servers/rest_server.py" "${args[@]}" || status=1
  rm -f "$base"
  if [ "$sandbox" != no ]; then
    "$python" "$hpclib/servers/rest_server.py" --probe-sandbox ||
      echo "setup_agents: the test container failed on this node; check the messages above, and GET /sandbox once the tunnel runs" >&2
  fi
  return "$status"
}

# Runs ON THE REMOTE HOST: the owner token. MODE is check, install HASH
# (only if there is none), replace HASH or matches HASH; prints hashed,
# plaintext, missing, installed, replaced, match or differs.
function _hpclib_remote_owner_token {
  local f="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/rest_token"
  case "$1" in
    check)
      if [ ! -e "$f" ]; then echo missing
      elif head -c 7 "$f" | grep -q '^sha256:'; then echo hashed
      else echo plaintext
      fi ;;
    install)
      mkdir -p "$(dirname "$f")" &&
        (umask 077; set -o noclobber; printf 'sha256:%s\n' "$2" > "$f") && echo installed ;;
    replace)
      mkdir -p "$(dirname "$f")" &&
        (umask 077; printf 'sha256:%s\n' "$2" > "$f.new" && mv "$f.new" "$f") && echo replaced ;;
    matches)  # is the owner token the one with hash $2?
      if [ -e "$f" ] && [ "$(head -n 1 "$f" | tr -d '[:space:]')" = "sha256:$2" ]; then echo match; else echo differs; fi ;;
  esac
}

# Run one of the _hpclib_remote_* functions on the cluster with ARGS, and
# with the contents of INPUT_FILE (if not empty) on its stdin. Like
# install_hpclib, the code goes over stdin to `bash -s`, so the remote
# command line is plain words whatever the login shell is.
function _hpclib_agents_remote {  # _hpclib_agents_remote FUNCTION INPUT_FILE ARGS...
  local fn="$1" input="$2" remote_command delimiter="HPCLIB_INPUT_$RANDOM$RANDOM"
  shift 2
  printf -v remote_command '%q ' bash -s -- "$@"
  {
    declare -f "$fn"
    if [ -n "$input" ]; then
      printf '%s "$@" <<'"'"'%s'"'"'\n' "$fn" "$delimiter"
      cat "$input"
      printf '\n%s\n' "$delimiter"
    else
      printf '%s "$@" < /dev/null\n' "$fn"
    fi
    printf 'exit $?\n'
  } | HPCLIB_ECHO_COMMANDS= pssh "${_hpclib_agents_login[@]}" "$remote_command"
}

function _hpclib_sha256 {  # the sha256 of a token file's contents, without trailing whitespace
  python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.read().strip().encode()).hexdigest())' < "$1"
}

function _hpclib_agent_profiles {  # the profile store: lib/agent_profiles.py
  python3 "$HPCLIB_DIR/lib/agent_profiles.py" "$@"
}

function _hpclib_agent_lines {  # _hpclib_agent_lines NAME KEY: a list-valued profile key, one item per line
  _hpclib_agent_profiles get "$1" "$2" 2>/dev/null || true
}

# The Python that will run rest_mcp.py: $HPCLIB_MCP_PYTHON, or the first
# python3 that has the MCP SDK. Prints the path; returns 1 (printing plain
# python3's path) if none has it.
function _hpclib_mcp_python {
  local candidate
  for candidate in "${HPCLIB_MCP_PYTHON:-}" "$(command -v python3)" /usr/local/bin/python3 /opt/homebrew/bin/python3; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if "$candidate" -c 'import mcp' > /dev/null 2>&1; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf '%s\n' "${HPCLIB_MCP_PYTHON:-$(command -v python3)}"
  return 1
}

# Terminal colours for the getting-started block: on for a terminal, off
# for pipes and files, NO_COLOR, or HPCLIB_COLOR=never (HPCLIB_COLOR=always
# forces them on).
function _hpclib_colors {
  if [ "${HPCLIB_COLOR:-auto}" = always ] ||
      { [ "${HPCLIB_COLOR:-auto}" = auto ] && [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; }; then
    _c_reset=$'\033[0m' _c_banner=$'\033[1;97;44m' _c_head=$'\033[1;36m' _c_cmd=$'\033[32m'
    _c_json=$'\033[33m' _c_dim=$'\033[2m' _c_warn=$'\033[1;33m' _c_alert=$'\033[1;31m'
  else
    _c_reset='' _c_banner='' _c_head='' _c_cmd='' _c_json='' _c_dim='' _c_warn='' _c_alert=''
  fi
}

function _hpclib_agents_getting_started {  # _hpclib_agents_getting_started PROFILE_NAME
  local name="$1" host port process_port mcp_name token_file pdir mcp_python mcp_ok=yes local_hpclib
  local _c_reset _c_banner _c_head _c_cmd _c_json _c_dim _c_warn _c_alert line d allow='' roots=''
  _hpclib_colors
  host=$(_hpclib_agent_profiles get "$name" host)
  port=$(_hpclib_agent_profiles get "$name" port)
  process_port=$(_hpclib_agent_profiles get "$name" process_port)
  mcp_name=$(_hpclib_agent_profiles get "$name" mcp_name)
  token_file=$(_hpclib_agent_profiles get "$name" token_file)
  pdir=$(_hpclib_agent_profiles dir "$name")
  mcp_python=$(_hpclib_mcp_python) || mcp_ok=no
  local_hpclib=$(cd -P "$HPCLIB_DIR" 2>/dev/null && pwd)
  while IFS= read -r d; do
    [ -n "$d" ] && allow="$allow --allow $d"
  done < <(_hpclib_agent_lines "$name" work_dirs)
  while IFS= read -r d; do
    [ -n "$d" ] && roots="$roots${roots:+, }$d"
  done < <(_hpclib_agent_lines "$name" local_roots)

  printf '\n%s  hpclib agents: %s is ready  %s\n\n' "$_c_banner" "$host" "$_c_reset"
  printf '%sProfile%s  %s\n' "$_c_head" "$_c_reset" "$pdir"
  printf '%s         ports %s (your machine and the login node) and %s (the compute node), picked at random%s\n' \
    "$_c_dim" "$port" "$process_port" "$_c_reset"
  printf '%s         local folders the agent may push from and pull into: %s%s\n\n' \
    "$_c_dim" "${roots:-none (rerun with --local-root DIR)}" "$_c_reset"

  local where="it is a SLURM job" login_node=''
  if [ "$(_hpclib_agent_profiles get "$name" rest_on 2>/dev/null)" = login ]; then
    where="the server runs on the login node, not in a job" login_node=' --login-node'
  fi
  printf '%s1. Start the tunnel%s (leave it running; %s)\n' "$_c_head" "$_c_reset" "$where"
  printf '   %sagent_tunnel %s%s\n' "$_c_cmd" "$name" "$_c_reset"
  printf '%s   = launch_tunnel -A none -P %s %s rest --process-port=%s%s --%s%s\n\n' \
    "$_c_dim" "$port" "$host" "$process_port" "$login_node" "$allow" "$_c_reset"

  printf '%s2. REQUIRED: add the MCP server to your LLM client.%s setup_agents does not change the client'"'"'s config, and\n' \
    "$_c_alert" "$_c_reset"
  printf '   %suntil you do, the agent has no tools for this cluster (or an old entry'"'"'s port and token).%s\n' \
    "$_c_alert" "$_c_reset"
  printf '   Claude Desktop: merge into "mcpServers" in ~/Library/Application Support/Claude/claude_desktop_config.json\n'
  printf '   (also saved as %s/mcp.json):\n\n' "$pdir"
  while IFS= read -r line; do
    printf '%s%s%s\n' "$_c_json" "$line" "$_c_reset"
  done < <(_hpclib_agent_profiles mcp "$name" "$mcp_python" "$local_hpclib/servers/rest_mcp.py")
  printf '\n   Claude Code (remove an older entry first with `claude mcp remove %s`):\n' "$mcp_name"
  printf '   %sclaude mcp add-json %s '"'"'%s'"'"'%s\n' "$_c_alert" "$mcp_name" \
    "$(_hpclib_agent_profiles mcp-entry "$name" "$mcp_python" "$local_hpclib/servers/rest_mcp.py")" "$_c_reset"
  printf '   %sThen quit the client completely (Claude Desktop: Cmd-Q, not just closing the window) and reopen it.%s\n' \
    "$_c_alert" "$_c_reset"
  if [ "$mcp_ok" = no ]; then
    printf '   %s%s has no MCP SDK: run `%s -m pip install mcp`, or set HPCLIB_MCP_PYTHON and rerun%s\n' \
      "$_c_warn" "$mcp_python" "$mcp_python" "$_c_reset"
  fi
  printf '\n%s3. Check it%s (with the tunnel up)\n' "$_c_head" "$_c_reset"
  printf '   %scurl -s -H "Authorization: Bearer $(cat %s)" http://127.0.0.1:%s/health%s\n' \
    "$_c_cmd" "$token_file" "$port" "$_c_reset"
  printf '   then ask the agent to run sandbox_info and cluster_info\n\n'
  printf '%s4. Later%s\n' "$_c_head" "$_c_reset"
  printf '   stop the tunnel:           %sagent_stop %s%s\n' "$_c_cmd" "$name" "$_c_reset"
  printf '   clusters on this machine:  %sagent_list%s\n' "$_c_cmd" "$_c_reset"
  printf '   for scripts (RESTClient):  %seval "$(agent_env %s)"%s\n' "$_c_cmd" "$name" "$_c_reset"
  printf '   rerun, keeping everything: %ssetup_agents %s%s   (--rebuild replaces it all)\n' \
    "$_c_cmd" "$name" "$_c_reset"
  printf '   describe the cluster for agents: cluster_notes in ~/.local/tunnels/rest/config.json on the cluster\n\n'
  printf '%sNot done yet: add the MCP entry from step 2 to your LLM client and restart it.%s\n\n' "$_c_alert" "$_c_reset"
}

# Prepare a cluster for agents (LLM clients) using the REST server's job
# templates, from your own machine, over the same connection as pssh:
#
#   setup_agents --work-dir /scratch/user/me/llm [options] [ssh options] [user@]host
#
#  1. installs or upgrades hpclib there (install_hpclib)
#  2. copies the bundled job templates into ~/.local/tunnels/rest/templates
#  3. writes the REST server's config.json, with a job sandbox: every
#     template job runs in Singularity/Apptainer and can write only to the
#     token's directories. Module trees on the cluster's MODULEPATH are made
#     readable, and the sandbox is tested on the login node
#  4. creates the owner (full-access) token on this machine and gives the
#     cluster only its hash
#  5. mints a scoped token limited to the --work-dir directories
#  6. prints how to start the tunnel and the MCP client entry
#
# Everything about the cluster is kept in a profile, ~/.config/hpclib/agents/
# [user@]host/ (agent_profiles.py): the login, a pair of randomly chosen
# ports, the work directories, the tokens, the MCP server name and the
# local folders the agent may push and pull (by default ~/Documents/Claude,
# the Claude desktop app's working folder). Reruns
# and agent_tunnel/agent_stop read it, so later commands need only the
# address (or the profile name). Options given on a rerun update it.
#
# Rerunning it is safe: existing templates, config and tokens are kept,
# except that a config without a sandbox gets one. --rebuild replaces them
# all: the templates and config (old copies are kept on the cluster), the
# sandbox's host image, the owner token's hash, and the scoped token (the old
# one is revoked).
function setup_agents {
  local usage='usage: setup_agents [--work-dir DIR ...] [--bind DIR ...] [--local-root DIR ...] [--templates LIST|all]
       [--config FILE] [--no-sandbox] [--rebuild] [--no-install] [--target DIR] [--name NAME]
       [--no-local-root] [--python-module MODULE ...] [--remote-python PATH] [--port N] [--process-port N] [--new-ports] [--token-name NAME] [--token-file FILE]
       [--owner-token-file FILE] [--mcp-name NAME] [--scopes LIST] [ssh options] [user@]host'
  local work_dirs=() binds=() local_roots=() login_args=() hosts=()
  local name='' token_name='' token_file='' owner_file='' port='' process_port='' mcp_name='' new_ports=no
  local no_local_root=no python_modules=() remote_python=''
  local scopes="read,submit,propose,files:write,envs"
  local templates="hello,orca,python_project,writing_templates" base_config='' sandbox=yes rebuild=no install=yes
  local target="$HPCLIB_REMOTE_INSTALL_LOCATION"
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --work-dir|--token-name|--token-file|--owner-token-file|--scopes|--templates|--config|--bind|--target|\
--name|--port|--process-port|--mcp-name|--local-root|--python-module|--remote-python)
        if [ "$#" -lt 2 ] || [ -z "$2" ]; then
          echo "setup_agents: $1 needs a value" >&2
          return 2
        fi
        case "$1" in
          --work-dir) work_dirs+=("$2") ;;
          --token-name) token_name="$2" ;;
          --token-file) token_file="$2" ;;
          --owner-token-file) owner_file="$2" ;;
          --scopes) scopes="$2" ;;
          --templates) templates="$2" ;;
          --config) base_config="$2" ;;
          --bind) binds+=("$2") ;;
          --target) target="$2" ;;
          --name) name="$2" ;;
          --port) port="$2" ;;
          --process-port) process_port="$2" ;;
          --mcp-name) mcp_name="$2" ;;
          --local-root) local_roots+=("$2") ;;
          --python-module) python_modules+=("$2") ;;
          --remote-python) remote_python="$2" ;;
        esac
        shift 2 ;;
      --no-sandbox) sandbox=no; shift ;;
      --rebuild) rebuild=yes; shift ;;
      --no-install) install=no; shift ;;
      --new-ports) new_ports=yes; shift ;;
      --no-local-root) no_local_root=yes; shift ;;
      -h|--help) echo "$usage"; return 0 ;;
      *) login_args+=("$1"); shift ;;
    esac
  done
  if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 6))' 2> /dev/null; then
    echo "setup_agents: needs python3 (3.6 or newer) on this machine" >&2
    return 1
  fi
  hosts=($(mcargs "$SSH_FLAGS" "$SSH_LONG_FLAGS" "${login_args[@]}"))
  local d
  # `setup_agents NAME` (a profile name, or an address with a profile) reruns with the stored login
  if [ "${#hosts[@]}" -eq 1 ] && [ "${#login_args[@]}" -eq 1 ] && [ -z "$name" ]; then
    local found
    if found=$(_hpclib_agent_profiles find "${hosts[0]}") && [ -n "$found" ]; then
      name="$found"
      login_args=()
      while IFS= read -r d; do login_args+=("$d"); done < <(_hpclib_agent_lines "$name" login)
      hosts=($(mcargs "$SSH_FLAGS" "$SSH_LONG_FLAGS" "${login_args[@]}"))
    fi
  fi
  if [ "${#hosts[@]}" -ne 1 ]; then
    echo "$usage" >&2
    return 2
  fi
  for d in "${work_dirs[@]}" "${binds[@]}"; do
    case "$d" in
      /*) ;;
      *) echo "setup_agents: $d must be an absolute path on the cluster" >&2; return 2 ;;
    esac
  done
  for d in "${local_roots[@]}"; do
    [ -d "$d" ] || { echo "setup_agents: --local-root $d is not a directory on this machine" >&2; return 2; }
  done
  if [ -n "$base_config" ] && [ ! -f "$base_config" ]; then
    echo "setup_agents: $base_config does not exist" >&2
    return 2
  fi
  if [ "$templates" = all ]; then
    templates=$(cd "$HPCLIB_DIR/tunnels/rest/templates" && ls | paste -sd, -)
  fi

  # the profile: made on the first run, updated with whatever was given now
  [ -n "$name" ] || name=$(_hpclib_agent_profiles name "${hosts[0]}") || return 1
  local profile_state
  profile_state=$(_hpclib_agent_profiles init "$name" "${hosts[0]}") || return 1
  local updates=("host=${hosts[0]}" "login=")
  for d in "${login_args[@]}"; do updates+=("login+=$d"); done
  if [ "${#work_dirs[@]}" -gt 0 ]; then
    updates+=("work_dirs=")
    for d in "${work_dirs[@]}"; do updates+=("work_dirs+=$d"); done
  fi
  if [ "${#binds[@]}" -gt 0 ]; then
    updates+=("binds=")
    for d in "${binds[@]}"; do updates+=("binds+=$d"); done
  fi
  if [ "${#local_roots[@]}" -gt 0 ]; then
    updates+=("local_roots=" "local_roots_off=no")
    for d in "${local_roots[@]}"; do updates+=("local_roots+=$(cd -P "$d" && pwd)"); done
  elif [ "$no_local_root" = yes ]; then
    updates+=("local_roots=" "local_roots_off=yes")
  elif [ -z "$(_hpclib_agent_lines "$name" local_roots)" ] &&
      [ "$(_hpclib_agent_profiles get "$name" local_roots_off 2>/dev/null)" != yes ]; then
    # By default the agent may use the Claude desktop app's own working folder.
    d="${HPCLIB_AGENT_LOCAL_ROOT:-$HOME/Documents/Claude}"
    if mkdir -p "$d" 2> /dev/null; then
      updates+=("local_roots=$(cd -P "$d" && pwd)")
    fi
  fi
  [ -n "$token_name" ] && updates+=("token_name=$token_name")
  [ -n "$token_file" ] && updates+=("token_file=$token_file")
  [ -n "$owner_file" ] && updates+=("owner_token_file=$owner_file")
  [ -n "$port" ] && updates+=("port=$port")
  [ -n "$process_port" ] && updates+=("process_port=$process_port")
  [ -n "$mcp_name" ] && updates+=("mcp_name=$mcp_name")
  if [ "${#python_modules[@]}" -gt 0 ]; then
    updates+=("python_spec=modules:${python_modules[*]}")
  elif [ -n "$remote_python" ]; then
    updates+=("python_spec=path:$remote_python")
  fi
  _hpclib_agent_profiles set "$name" "${updates[@]}" || return 1
  if [ "$new_ports" = yes ]; then
    _hpclib_agent_profiles new-ports "$name" || return 1
  fi
  work_dirs=() binds=()
  while IFS= read -r d; do [ -n "$d" ] && work_dirs+=("$d"); done < <(_hpclib_agent_lines "$name" work_dirs)
  while IFS= read -r d; do [ -n "$d" ] && binds+=("$d"); done < <(_hpclib_agent_lines "$name" binds)
  if [ "${#work_dirs[@]}" -eq 0 ]; then
    echo "setup_agents: give at least one --work-dir (a directory on the cluster the agent may use)" >&2
    return 2
  fi
  token_name=$(_hpclib_agent_profiles get "$name" token_name)
  token_file=$(_hpclib_agent_profiles get "$name" token_file)
  owner_file=$(_hpclib_agent_profiles get "$name" owner_token_file)
  token_file="${token_file/#\~/$HOME}" owner_file="${owner_file/#\~/$HOME}"
  echo "== profile $name ($( [ "$profile_state" = created ] && echo new || echo existing ))"

  local _hpclib_agents_login=("${login_args[@]}")
  local remote_hpclib
  remote_hpclib=$(_hpclib_remote_path "$target")
  local rest_server="$remote_hpclib/servers/rest_server.py"

  if [ "$install" = yes ]; then
    echo "== installing hpclib"
    install_hpclib --target "$target" "${login_args[@]}" || return 1
  fi

  echo "== Python on the cluster (3.9 or newer, for hpclib's servers)"
  local python_spec python_line
  python_spec=$(_hpclib_agent_profiles get "$name" python_spec 2>/dev/null || true)
  # an explicit choice made now is applied now; a stored one only on --rebuild or if the launcher stopped working
  if [ "${#python_modules[@]}" -eq 0 ] && [ -z "$remote_python" ] && [ "$rebuild" != yes ]; then
    python_line=$(_hpclib_agents_remote _hpclib_remote_python "" no "" | tail -n 1)
    if [ "${python_line%%|*}" != python ] && [ -n "$python_spec" ]; then
      python_line=$(_hpclib_agents_remote _hpclib_remote_python "" yes "$python_spec" | tail -n 1)
    fi
  else
    python_line=$(_hpclib_agents_remote _hpclib_remote_python "" yes "$python_spec" | tail -n 1)
  fi
  case "$python_line" in
    python\|*)
      local python_version="${python_line#python|}"
      echo "using Python ${python_version%%|*} (${python_version#*|})"
      _hpclib_agent_profiles set "$name" "remote_python=${python_version%%|*} (${python_version#*|})" || return 1 ;;
    *)
      echo "setup_agents: could not set up a Python for hpclib on the cluster" >&2
      return 1 ;;
  esac

  echo "== templates and server config"
  local bind_list
  bind_list=$(IFS=,; printf '%s' "${binds[*]}")
  _hpclib_agents_remote _hpclib_remote_setup_agents "$base_config" "$remote_hpclib" "$rebuild" "$sandbox" \
    "$templates" "$bind_list" "${work_dirs[@]}" || return 1

  # A machine without SLURM (a development server) runs the server itself: its tunnel can't submit a job
  if [ -z "$(_hpclib_agent_profiles get "$name" rest_on 2>/dev/null)" ] &&
      [ "$(HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" "bash -lc 'command -v sbatch > /dev/null 2>&1 && echo slurm || echo none'" \
        < /dev/null 2> /dev/null | tail -n 1)" = none ]; then
    _hpclib_agent_profiles set "$name" rest_on=login || return 1
    echo "no SLURM on $(_hpclib_agent_profiles get "$name" host): its REST server will run on the machine itself" \
      "(rest_on=login), and so will jobs"
  fi

  echo "== owner token"
  local state hash result legacy="$HOME/.config/hpclib/rest_token"
  state=$(_hpclib_agents_remote _hpclib_remote_owner_token "" check | tail -n 1)
  case "$state" in
    missing|hashed|plaintext) ;;
    *) echo "setup_agents: could not check the owner token on the cluster (got: $state)" >&2; return 1 ;;
  esac
  # From before profiles: the cluster's owner token may be in ~/.config/hpclib/rest_token.
  if [ "$state" = hashed ] && [ ! -e "$owner_file" ] && [ -s "$legacy" ] && [ "$legacy" != "$owner_file" ] &&
      [ "$(_hpclib_agents_remote _hpclib_remote_owner_token "" matches "$(_hpclib_sha256 "$legacy")" | tail -n 1)" = match ]; then
    mkdir -p "$(dirname "$owner_file")"
    (umask 077; cp "$legacy" "$owner_file") && echo "copied this cluster's owner token from $legacy into the profile"
  fi
  if [ "$state" = missing ] || [ "$rebuild" = yes ]; then
    if [ ! -e "$owner_file" ]; then
      mkdir -p "$(dirname "$owner_file")"
      (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$owner_file") || return 1
      echo "created $owner_file"
    fi
    hash=$(_hpclib_sha256 "$owner_file") || return 1
    if [ "$state" = missing ]; then
      result=$(_hpclib_agents_remote _hpclib_remote_owner_token "" install "$hash" | tail -n 1)
    else
      result=$(_hpclib_agents_remote _hpclib_remote_owner_token "" replace "$hash" | tail -n 1)
    fi
    case "$result" in
      installed|replaced) ;;
      *) echo "setup_agents: could not install the owner token's hash" >&2; return 1 ;;
    esac
    echo "the cluster has the hash of $owner_file; keep that file, it is the only copy of the token"
    [ "$state" = plaintext ] && echo "  (the plaintext owner token that was on the cluster no longer works)"
  elif [ "$state" = hashed ]; then
    echo "the cluster already has a hashed owner token"
    [ -e "$owner_file" ] || echo "  ($owner_file doesn't exist; if you have lost the token, rerun with --rebuild)"
  else
    echo "the cluster has a plaintext owner token, made when the REST server first started; rerun with --rebuild"
    echo "to replace it with one kept on this machine, or hash it in place:"
    echo "  (umask 077; ssh ${login_args[*]} cat .local/tunnels/rest_token > $owner_file)"
    echo "  ssh ${login_args[*]} python3 $(case "$rest_server" in /*) printf %s "$rest_server" ;; *) printf '~/%s' "$rest_server" ;; esac) --hash-token-file"
  fi

  echo "== agent token '$token_name' for ${work_dirs[*]}"
  # From before profiles: the agent token may be in ~/.config/hpclib/llm_token. Keep using it
  # (and its name) if this cluster knows it.
  local legacy_agent="$HOME/.config/hpclib/llm_token" known
  if [ ! -e "$token_file" ] && [ "$rebuild" != yes ] && [ -s "$legacy_agent" ] && [ "$legacy_agent" != "$token_file" ]; then
    known=$(HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" \
      "$(_hpclib_remote_python_cmd "$rest_server" --lookup-token-hash "$(_hpclib_sha256 "$legacy_agent")")" \
      < /dev/null 2>/dev/null | tail -n 1)
    if [ -n "$known" ]; then
      mkdir -p "$(dirname "$token_file")"
      (umask 077; cp "$legacy_agent" "$token_file") || return 1
      token_name="$known"
      _hpclib_agent_profiles set "$name" "token_name=$token_name" || return 1
      echo "copied this cluster's '$token_name' token from $legacy_agent into the profile"
    fi
  fi
  if [ -e "$token_file" ] && [ "$rebuild" != yes ]; then
    echo "$token_file already exists; rerun with --rebuild to replace it"
    # tokens from earlier versions get the scopes added since (e.g. envs); none are taken away
    local granted
    granted=$(HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" \
      "$(_hpclib_remote_python_cmd "$rest_server" --add-token-scopes "$(_hpclib_sha256 "$token_file")" --scopes "$scopes")" \
      < /dev/null 2>/dev/null | tail -n 1)
    case "$granted" in
      *": added "*) echo "$granted" ;;
      *"already has"*) ;;
      *) echo "  (could not check its scopes; rerun with --rebuild if the agent lacks one of: $scopes)" ;;
    esac
  else
    local mint=(--add-token "$token_name" --scopes "$scopes")
    for d in "${work_dirs[@]}"; do
      mint+=(--token-allow "$d")
    done
    if [ "$rebuild" = yes ]; then
      # The old tokens stop working now: the one in the token file, whatever
      # its name, and any token with this name.
      local old_name
      if [ -s "$token_file" ]; then
        old_name=$(HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" \
          "$(_hpclib_remote_python_cmd "$rest_server" --revoke-token-hash "$(_hpclib_sha256 "$token_file")")" \
          < /dev/null 2>/dev/null | tail -n 1)
        case "$old_name" in
          "revoked "*) echo "$old_name (the token in $token_file)" ;;
        esac
      fi
      HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" "$(_hpclib_remote_python_cmd "$rest_server" --revoke-token "$token_name")" \
        < /dev/null > /dev/null 2>&1 && echo "revoked the old '$token_name' token"
    fi
    mkdir -p "$(dirname "$token_file")"
    local tmp
    tmp=$(umask 077; mktemp "$token_file.XXXXXX") || return 1
    if ! HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" "$(_hpclib_remote_python_cmd "$rest_server" "${mint[@]}")" < /dev/null > "$tmp" 2>/dev/null ||
        [ ! -s "$tmp" ]; then
      rm -f "$tmp"
      echo "setup_agents: could not mint the '$token_name' token" >&2
      return 1
    fi
    mv "$tmp" "$token_file"
    echo "saved to $token_file"
  fi

  _hpclib_agents_getting_started "$name"
}

# Start the REST tunnel for a cluster set up with setup_agents, with the
# profile's ports and directories:
#   agent_tunnel NAME|[user@]host [--review-templates|--auto-approve-templates=new] [launch_tunnel options]
# Template proposals from the agent are approved automatically, replacements
# included (the server only does that while jobs are sandboxed);
# --auto-approve-templates=new still holds replacements for your review, and
# --review-templates holds every proposal. Other options (e.g. --time=2:00:00)
# go to launch_tunnel.
function agent_tunnel {
  local name host port process_port d allow=() launch=() approve
  if [ "$#" -lt 1 ]; then
    echo "usage: agent_tunnel NAME|[user@]host [--review-templates|--auto-approve-templates=new] [launch_tunnel options]" >&2
    _hpclib_agent_profiles list >&2
    return 2
  fi
  name=$(_hpclib_agent_profiles find "$1") || {
    echo "agent_tunnel: no agent profile for '$1'; run setup_agents first (agent_list shows the profiles)" >&2
    return 1
  }
  shift
  host=$(_hpclib_agent_profiles get "$name" host)
  port=$(_hpclib_agent_profiles get "$name" port)
  process_port=$(_hpclib_agent_profiles get "$name" process_port)
  while IFS= read -r d; do [ -n "$d" ] && allow+=(--allow "$d"); done < <(_hpclib_agent_lines "$name" work_dirs)
  # the profile's tunnel settings (the console's settings page); options given here win
  approve=$(_hpclib_agent_profiles get "$name" auto_approve_templates 2>/dev/null || echo all)
  [ "$approve" = review ] && approve=''
  while IFS= read -r d; do [ -n "$d" ] && launch+=("$d"); done < <(_hpclib_agent_lines "$name" tunnel_args)
  # where the server runs (the profile's rest_on): in a job (default), or on the login node
  if [ "$(_hpclib_agent_profiles get "$name" rest_on 2>/dev/null)" = login ]; then
    launch=(--login-node)   # no job, so no sbatch options
  fi
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --auto-approve-templates|--auto-approve-templates=new) approve=new ;;
      --auto-approve-templates=all) approve=all ;;
      --review-templates) approve='' ;;
      --auto-approve-templates=*)
        echo "agent_tunnel: --auto-approve-templates takes new or all" >&2
        return 2 ;;
      *) launch+=("$1") ;;
    esac
    shift
  done
  # an option for the REST server, not launch_tunnel; it applies only while jobs are sandboxed
  [ -n "$approve" ] && allow+=("--auto-approve-templates=$approve")
  # if this ssh is the one that logs in, keep the login as long as the profile says (default 12h)
  local hours
  hours=$(_hpclib_agent_profiles get "$name" connection_hours 2>/dev/null) || hours="${HPCLIB_SSH_PERSIST%h}"
  HPCLIB_SSH_PERSIST="${hours:-12}h" launch_tunnel -A none -P "$port" "$host" rest "--process-port=$process_port" "${launch[@]}" -- "${allow[@]}"
}

# Stop a cluster's agent tunnel:  agent_stop NAME|[user@]host
function agent_stop {
  local name
  name=$(_hpclib_agent_profiles find "${1:-}") || {
    echo "agent_stop: no agent profile for '${1:-}' (agent_list shows the profiles)" >&2
    return 1
  }
  stop_tunnel -P "$(_hpclib_agent_profiles get "$name" port)" "$(_hpclib_agent_profiles get "$name" host)"
}

# Point scripts that use RESTClient.from_env() (e.g. run_scan.py) at a
# cluster's agent tunnel:  eval "$(agent_env NAME|[user@]host)"
function agent_env {
  local name
  name=$(_hpclib_agent_profiles find "${1:-}") || {
    echo "agent_env: no agent profile for '${1:-}' (agent_list shows the profiles)" >&2
    return 1
  }
  _hpclib_agent_profiles env "$name"
}

# The clusters set up with setup_agents on this machine.
function agent_list {
  _hpclib_agent_profiles list
}

# The Agent Console backend: a local HTTP API (127.0.0.1 only, with a session
# key) for a front end to watch tunnels, jobs, proposals and the audit log of
# the clusters set up with setup_agents.
#   agent_console [--port N] [--static DIR [--open]] [--allow-origin ORIGIN]
function agent_console {
  python3 "$HPCLIB_DIR/servers/agent_console.py" "$@"
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

# Everything hpclib starts for a port carries HPCLIB_TUNNEL_PORT=PORT in its
# environment (start_tunnel.sh and smbshell gui export it), and so does what
# that starts: podman's port forwarder, rclone, the ssh forward, the waiting
# page. Whatever is listening on the port is found from the kernel's socket
# tables, so a leftover is recognized however it was started or orphaned.
#   _hpclib_port_holders PORT [--stop]
# lists the processes listening on PORT (on stderr; 1 if there are none), and
# with --stop ends the ones that are yours and hpclib's for PORT: by that mark,
# or (for those started before it) rclone's web GUI from smbshell. TERM, then
# KILL after 5 s; 0 if it stopped any.
function _hpclib_port_holders {
  python3 - "$1" "${2:-}" <<'PY'
import os, signal, sys, time
port, stop = int(sys.argv[1]), sys.argv[2] == "--stop"
inodes = set()
for table in ("/proc/net/tcp", "/proc/net/tcp6"):
    try:
        with open(table) as f:
            next(f)
            for line in f:
                fields = line.split()
                if fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:   # LISTEN on PORT
                    inodes.add(f"socket:[{fields[9]}]")
    except OSError:
        pass
if not inodes:
    sys.exit(1)
me, mine, seen = os.getuid(), [], 0
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:                     # only your own processes' sockets and environment can be read
        if not any(os.readlink(f"/proc/{pid}/fd/{fd}") in inodes for fd in os.listdir(f"/proc/{pid}/fd")):
            continue
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = " ".join(f.read().replace(b"\0", b" ").decode(errors="replace").split())
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read().split(b"\0")
    except OSError:
        continue
    seen += 1
    marked = f"HPCLIB_TUNNEL_PORT={port}".encode() in env
    legacy = "rclone" in args and " rcd " in f" {args} " and "hpclib-rclone-gui." in args
    ours = os.stat(f"/proc/{pid}").st_uid == me and (marked or legacy)
    if ours:
        mine.append(int(pid))
    if not stop or ours:
        print(f"  {'stopping ' if stop else ''}pid {pid}: {args[:200]}"
              f"{'' if ours else ' (not an hpclib tunnel of yours)'}", file=sys.stderr)
if not stop:
    if not seen:
        print("  a process of another user's (or one you can't see)", file=sys.stderr)
    sys.exit(0)
if not mine:
    sys.exit(1)
for pid in mine:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
deadline = time.time() + 5
while time.time() < deadline and any(os.path.exists(f"/proc/{pid}") for pid in mine):
    time.sleep(0.1)
for pid in mine:
    try:
        os.kill(pid, signal.SIGKILL)
        print(f"  pid {pid} ignored TERM; killed", file=sys.stderr)
    except ProcessLookupError:
        pass
print(f"stopped what an earlier hpclib session left listening on port {port}", file=sys.stderr)
PY
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
    # still held: by something an earlier session left (orphaned, or started outside a tunnel, such as rclone's
    # web GUI or a podman forwarder), found by what is listening rather than by a record
    if [ "$tries" = 2 ]; then _hpclib_port_holders "$port" --stop && continue; fi
    sleep 0.5
  done
  echo "port $port on $(hostname -s) is in use by something that isn't one of your tunnels:" >&2
  _hpclib_port_holders "$port" || ss -ltnp "sport = :$port" 2>/dev/null | tail -n +2 >&2 || true
  echo "pick another port with -P (or stop that process, if it is yours and you are done with it)" >&2
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
  script="$(declare -f _hpclib_sessions_root _hpclib_port_file _hpclib_port_free _hpclib_port_holders _hpclib_clear_port)
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

          remote_command=$(_hpclib_remote_script_cmd /bin/bash "$remote_hpclib/tunnels/start_tunnel.sh" "$tunnel" \
            -P "$port" "${remote_args[@]}")
          printf '%s\n' "pssh -t -L 127.0.0.1:$port:127.0.0.1:$port $address \"$remote_command\""
          pssh -t -L "127.0.0.1:$port:127.0.0.1:$port" "$address" "$remote_command"
      fi
  fi
}
