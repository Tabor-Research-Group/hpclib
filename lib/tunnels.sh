

function _launch_app {
  local default_path="$1"
  shift
  local fallback_name="$1"
  shift
  local cmd
  local proc_id

  if [ -f "$default_path" ];
    then
      echo $("$default_path" $@)
    else
      echo $(open -na "$fallback_name" --args "$@")
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
  echo $(_launch_app "$DEFAULT_CHROMIUM_LAUNCH_PATH" "Chromium" "--app=$app" $@)
}

function _launch_safari {
  local browser_mode="$1"
  shift
  echo $(_launch_app "$DEFAULT_SAFARI_LAUNCH_PATH" "Safari" $@)
}

function _launch_firefox {
  local browser_mode="$1"
  shift
  echo $(_launch_app "$DEFAULT_FIREFOX_LAUNCH_PATH" "Firefox" $@)
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

LAUNCH_TUNNEL_ARGS="bP:A:"
function launch_tunnel {
  local port=$(mcoptvalue "$LAUNCH_TUNNEL_ARGS" "P" $@);
  local app=$(mcoptvalue "$LAUNCH_TUNNEL_ARGS" "A" $@);
  local browser_mode=$(mcoptvalue "$LAUNCH_TUNNEL_ARGS" "b" $@);
  local args=$(mcargs "$LAUNCH_TUNNEL_ARGS" $@);
  args=($args)
  local address="${args[0]}"
  local tunnel="${args[1]}"
  local launch_args
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

          if [ "-z" "$app" ]; then
            app="CodeLauncher"
          fi

          PS1="\u\@$address-$TUNNEL\$ "
          PROMPT_COMMAND="echo -ne \"\033]0;$address-$TUNNEL: \${PWD}\007\""
          echo -ne "\033]0;$address-$TUNNEL\007"

          psync -r $HPCLIB_DIR $address:hpclib

          launcher=$(locate_browser_launcher "$app")
          launch_args=${args[@]:2}
          $launcher "$browser_mode" http://localhost:$port ${launch_args[@]}
          pssh -t -L 127.0.0.1:$port:127.0.0.1:$port $address "/bin/bash hpclib/tunnels/start_tunnel.sh ${tunnel} -P $port"
      fi
  fi
}