

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

LAUNCH_TUNNEL_DEFAULT_APP="Safari"
LAUNCH_TUNNEL_ARGS="bP:A:"
LAUNCH_TUNNEL_LONG_ARGS="browser-arg:"
LAUNCH_TUNNEL_RSYNC="false"
function launch_tunnel {
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
  local launcher

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

          local quoted_remote_args
          printf -v quoted_remote_args '%q ' "${remote_args[@]}"
          printf "%s\n" "pssh -t -L 127.0.0.1:$port:127.0.0.1:$port $address \"/bin/bash hpclib/tunnels/start_tunnel.sh ${tunnel} -P $port ${quoted_remote_args}\""
          pssh -t -L 127.0.0.1:$port:127.0.0.1:$port $address "/bin/bash hpclib/tunnels/start_tunnel.sh ${tunnel} -P $port ${quoted_remote_args}"
      fi
  fi
}