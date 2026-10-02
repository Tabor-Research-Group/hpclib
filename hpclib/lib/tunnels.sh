

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

# Runs ON THE REMOTE HOST: copy bundled templates and write the REST
# server's config. Arguments: HPCLIB_DIR REBUILD(yes|no) SANDBOX(yes|no)
# TEMPLATES(comma list) BINDS(comma list) WORK_DIR... ; stdin is a base
# config (JSON), or empty.
function _hpclib_remote_setup_agents {
  local hpclib="$1" rebuild="$2" sandbox="$3" templates="$4" binds="$5"
  shift 5
  local data="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/rest"
  local stamp t d base status=0
  local args=(--init-config)
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
  python3 "$hpclib/servers/rest_server.py" "${args[@]}" || status=1
  rm -f "$base"
  if [ "$sandbox" != no ]; then
    python3 "$hpclib/servers/rest_server.py" --probe-sandbox ||
      echo "setup_agents: the test container failed on this node; check the messages above, and GET /sandbox once the tunnel runs" >&2
  fi
  return "$status"
}

# Runs ON THE REMOTE HOST: the owner token. MODE is check, install HASH
# (only if there is none) or replace HASH; prints hashed, plaintext,
# missing, installed or replaced.
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
#  5. mints a scoped token limited to the --work-dir directories, saved on
#     this machine (mode 600)
#
# Rerunning it is safe: existing templates, config and tokens are kept,
# except that a config without a sandbox gets one. --rebuild replaces them
# all: the templates and config (old copies are kept on the cluster), the
# sandbox's host image, the owner token's hash, and the scoped token (the old
# one is revoked).
function setup_agents {
  local usage='usage: setup_agents --work-dir DIR [--work-dir DIR ...] [--token-name NAME] [--token-file FILE]
       [--owner-token-file FILE] [--scopes LIST] [--templates LIST|all] [--config FILE]
       [--bind DIR ...] [--no-sandbox] [--rebuild] [--no-install] [--target DIR] [ssh options] [user@]host'
  local work_dirs=() binds=() login_args=() hosts=()
  local token_name="llm" token_file="$HOME/.config/hpclib/llm_token"
  local owner_file="$HOME/.config/hpclib/rest_token" scopes="read,submit,propose,files:write"
  local templates="hello,orca,writing_templates" base_config='' sandbox=yes rebuild=no install=yes
  local target="$HPCLIB_REMOTE_INSTALL_LOCATION" token_custom=false
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --work-dir|--token-name|--token-file|--owner-token-file|--scopes|--templates|--config|--bind|--target)
        if [ "$#" -lt 2 ] || [ -z "$2" ]; then
          echo "setup_agents: $1 needs a value" >&2
          return 2
        fi
        case "$1" in
          --work-dir) work_dirs+=("$2") ;;
          --token-name) token_name="$2" ;;
          --token-file) token_file="$2"; token_custom=true ;;
          --owner-token-file) owner_file="$2" ;;
          --scopes) scopes="$2" ;;
          --templates) templates="$2" ;;
          --config) base_config="$2" ;;
          --bind) binds+=("$2") ;;
          --target) target="$2" ;;
        esac
        shift 2 ;;
      --no-sandbox) sandbox=no; shift ;;
      --rebuild) rebuild=yes; shift ;;
      --no-install) install=no; shift ;;
      -h|--help) echo "$usage"; return 0 ;;
      *) login_args+=("$1"); shift ;;
    esac
  done
  hosts=($(mcargs "$SSH_FLAGS" "$SSH_LONG_FLAGS" "${login_args[@]}"))
  if [ "${#hosts[@]}" -ne 1 ] || [ "${#work_dirs[@]}" -eq 0 ]; then
    echo "$usage" >&2
    return 2
  fi
  local d
  for d in "${work_dirs[@]}" "${binds[@]}"; do
    case "$d" in
      /*) ;;
      *) echo "setup_agents: $d must be an absolute path on the cluster" >&2; return 2 ;;
    esac
  done
  if [ -n "$base_config" ] && [ ! -f "$base_config" ]; then
    echo "setup_agents: $base_config does not exist" >&2
    return 2
  fi
  if [ "$templates" = all ]; then
    templates=$(cd "$HPCLIB_DIR/tunnels/rest/templates" && ls | paste -sd, -)
  fi
  [ "$token_custom" = true ] || [ "$token_name" = llm ] || token_file="$HOME/.config/hpclib/${token_name}_token"
  local _hpclib_agents_login=("${login_args[@]}")
  local remote_hpclib
  remote_hpclib=$(_hpclib_remote_path "$target")
  local rest_server="$remote_hpclib/servers/rest_server.py"

  if [ "$install" = yes ]; then
    echo "== installing hpclib"
    install_hpclib --target "$target" "${login_args[@]}" || return 1
  fi

  echo "== templates and server config"
  local bind_list work_list
  bind_list=$(IFS=,; printf '%s' "${binds[*]}")
  _hpclib_agents_remote _hpclib_remote_setup_agents "$base_config" "$remote_hpclib" "$rebuild" "$sandbox" \
    "$templates" "$bind_list" "${work_dirs[@]}" || return 1

  echo "== owner token"
  local state hash result
  state=$(_hpclib_agents_remote _hpclib_remote_owner_token "" check | tail -n 1)
  case "$state" in
    missing|hashed|plaintext) ;;
    *) echo "setup_agents: could not check the owner token on the cluster (got: $state)" >&2; return 1 ;;
  esac
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
    echo "  ssh ${login_args[*]} python3 $rest_server --hash-token-file"
  fi

  echo "== scoped token '$token_name' for ${work_dirs[*]}"
  if [ -e "$token_file" ] && [ "$rebuild" != yes ]; then
    echo "$token_file already exists; rerun with --rebuild to replace it"
  else
    local mint=(python3 "$rest_server" --add-token "$token_name" --scopes "$scopes")
    for d in "${work_dirs[@]}"; do
      mint+=(--token-allow "$d")
    done
    if [ "$rebuild" = yes ]; then
      # The old tokens stop working now: the one in the token file, whatever
      # its name, and any token with this name.
      local old_name
      if [ -s "$token_file" ]; then
        old_name=$(HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" \
          "$(printf '%q ' python3 "$rest_server" --revoke-token-hash "$(_hpclib_sha256 "$token_file")")" \
          < /dev/null 2>/dev/null | tail -n 1)
        case "$old_name" in
          "revoked "*) echo "$old_name (the token in $token_file)" ;;
        esac
      fi
      HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" "$(printf '%q ' python3 "$rest_server" --revoke-token "$token_name")" \
        < /dev/null > /dev/null 2>&1 && echo "revoked the old '$token_name' token"
    fi
    mkdir -p "$(dirname "$token_file")"
    local tmp
    tmp=$(umask 077; mktemp "$token_file.XXXXXX") || return 1
    if ! HPCLIB_ECHO_COMMANDS= pssh "${login_args[@]}" "$(printf '%q ' "${mint[@]}")" < /dev/null > "$tmp" ||
        [ ! -s "$tmp" ]; then
      rm -f "$tmp"
      echo "setup_agents: could not mint the '$token_name' token" >&2
      return 1
    fi
    mv "$tmp" "$token_file"
    echo "saved to $token_file"
  fi

  local allow_args='' mcp_python
  for d in "${work_dirs[@]}"; do
    allow_args="$allow_args --allow $d"
  done
  mcp_python=$(command -v python3)
  local local_hpclib
  local_hpclib=$(cd -P "$HPCLIB_DIR" 2>/dev/null && pwd)
  cat <<EOF

Next:
  - start the tunnel:  launch_tunnel -A none -P 5050 ${login_args[*]} rest --$allow_args
  - point your MCP client (e.g. Claude Desktop) at the server, with the MCP SDK installed for that Python:
      {"mcpServers": {"hpclib": {"command": "$mcp_python",
        "args": ["$local_hpclib/servers/rest_mcp.py", "--url", "http://127.0.0.1:5050",
                 "--token-file", "$token_file"]}}}
  - edit cluster_notes (and limits) in ~/.local/tunnels/rest/config.json on the cluster; the agent's
    sandbox_info tool reports what jobs can read once the tunnel is up
EOF
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
