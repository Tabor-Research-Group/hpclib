

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
            psync -r $HPCLIB_DIR $address:hpclib/
          fi

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

          printf -v remote_command '%q ' /bin/bash hpclib/tunnels/start_tunnel.sh "$tunnel" -P "$port" "${remote_args[@]}"
          printf '%s\n' "pssh -t -L 127.0.0.1:$port:127.0.0.1:$port $address \"$remote_command\""
          pssh -t -L "127.0.0.1:$port:127.0.0.1:$port" "$address" "$remote_command"
      fi
  fi
}
