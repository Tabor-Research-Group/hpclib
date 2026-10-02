#!/usr/bin/env bash
set -e

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_dir="$(mktemp -d)"
test_dir="$(cd -P "$test_dir" && pwd)"
trap 'rm -rf "$test_dir"' EXIT

HPCLIB_DIR="$repo_dir/hpclib"
HPCLIB_TUNNEL_INSTALL_LOCATION="$test_dir/installed tunnels"
source "$HPCLIB_DIR/hpclib.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_equal() { [ "$1" = "$2" ] || fail "expected '$2', got '$1'"; }

assert_equal "$(resolve_tunnel vscode)" "$HPCLIB_DIR/tunnels/vscode"
assert_equal "$(resolve_tunnel flask)" "$HPCLIB_DIR/tunnels/flask"
assert_equal "$HPCLIB_TUNNEL_PATH" "$HPCLIB_TUNNEL_INSTALL_LOCATION:$HPCLIB_DIR/tunnels"

mkdir -p "$test_dir/first/demo" "$test_dir/second/demo"
touch "$test_dir/first/demo/sbatch_script.sh" "$test_dir/second/demo/sbatch_script.sh"
touch "$test_dir/second/demo/tunnel_config.sh"
HPCLIB_TUNNEL_PATH="$test_dir/first:$test_dir/second:$HPCLIB_DIR/tunnels"
assert_equal "$(resolve_tunnel demo)" "$test_dir/first/demo"
assert_equal "$(resolve_tunnel_file tunnel_config.sh demo)" "$test_dir/second/demo/tunnel_config.sh"
assert_equal "$(resolve_tunnel_file configure_job.sh demo)" "$HPCLIB_DIR/tunnels/configure_job.sh"
if resolve_tunnel ../demo >/dev/null 2>&1; then fail 'accepted a path as a tunnel name'; fi

mkdir -p "$test_dir/home"
touch "$test_dir/home/.bashrc"
if HOME="$test_dir/home" HPCLIB_DIR="$HPCLIB_DIR" HPCLIB_TUNNEL_PATH="$test_dir/first" \
  bash "$HPCLIB_DIR/tunnels/start_tunnel.sh" missing > "$test_dir/start.log" 2>&1; then
  fail 'start_tunnel accepted a missing tunnel'
fi
grep -q 'not found in HPCLIB_TUNNEL_PATH' "$test_dir/start.log" || fail 'launcher did not use the resolver'

mkdir -p "$test_dir/downloaded/demo"
touch "$test_dir/downloaded/demo/sbatch_script.sh"
cat > "$test_dir/downloaded/demo/install.sh" <<'SCRIPT'
#!/usr/bin/env bash
echo 'running demo install.sh'
printf '%s\n' "$HPCLIB_TUNNEL_DIR" > installed_for.txt
SCRIPT
installed="$(install_tunnel --target "$HPCLIB_TUNNEL_INSTALL_LOCATION" "$test_dir/downloaded/demo")"
assert_equal "$installed" "$HPCLIB_TUNNEL_INSTALL_LOCATION/demo"
assert_equal "$(cat "$installed/installed_for.txt")" "$installed"
if install_tunnel "$test_dir/downloaded/demo" >/dev/null 2>&1; then fail 'overwrote an existing tunnel'; fi

mkdir -p "$test_dir/downloaded/broken"
touch "$test_dir/downloaded/broken/sbatch_script.sh"
printf '#!/usr/bin/env bash\nexit 7\n' > "$test_dir/downloaded/broken/install.sh"
if install_tunnel "$test_dir/downloaded/broken" >/dev/null 2>&1; then fail 'accepted a failed install.sh'; fi
[ ! -e "$HPCLIB_TUNNEL_INSTALL_LOCATION/broken" ] || fail 'left a failed installation'

mkdir -p "$test_dir/bin"
cat > "$test_dir/bin/singularity" <<'SCRIPT'
#!/usr/bin/env bash
[ "$1" = pull ] || exit 2
[ "$3" = docker://codercom/code-server:latest ] || exit 3
touch "$2"
SCRIPT
chmod +x "$test_dir/bin/singularity"
PATH="$test_dir/bin:$PATH"
export PATH
VSCODE_CONTAINER="$test_dir/code server/vscode.sif"
export VSCODE_CONTAINER
install_tunnel "$HPCLIB_DIR/tunnels/vscode" >/dev/null
[ -f "$VSCODE_CONTAINER" ] || fail 'VS Code image was not pulled to its configured path'
[ -f "$HPCLIB_TUNNEL_INSTALL_LOCATION/vscode/install.sh" ] || fail 'install.sh was not copied'

# The local launcher sends everything after -- as script arguments.
cat > "$test_dir/bin/ssh" <<'SCRIPT'
#!/usr/bin/env bash
after_host=false
remote=''
for ssh_arg in "$@"; do
  if [ "$after_host" = true ]; then
    remote="$remote${remote:+ }$ssh_arg"
  elif [ "$ssh_arg" = login.example ]; then
    after_host=true
  fi
done
printf '%s\n' "$remote" > "$TEST_SSH_REMOTE_FILE"
SCRIPT
chmod +x "$test_dir/bin/ssh"
(
  HOME="$test_dir/home"
  TEST_SSH_REMOTE_FILE="$test_dir/remote-command"
  export HOME TEST_SSH_REMOTE_FILE
  _wait_for_port() { return 1; }
  launch_tunnel -P 5050 login.example flask --mem=2gb -- \
    'mypackage:create_app("hello world")' >/dev/null 2>&1
)
remote_command="$(cat "$test_dir/remote-command")"
eval "set -- $remote_command"
last_arg=''
for last_arg in "$@"; do :; done
assert_equal "${6}" '--mem=2gb'
assert_equal "${7}" '--'
assert_equal "$last_arg" 'mypackage:create_app("hello world")'

# start_tunnel places those arguments after the script path in sbatch.
cat > "$test_dir/bin/sbatch" <<'SCRIPT'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$TEST_SBATCH_ARGS_FILE"
SCRIPT
cat > "$test_dir/bin/squeue" <<'SCRIPT'
#!/usr/bin/env bash
exit 0
SCRIPT
cat > "$test_dir/bin/python3" <<'SCRIPT'
#!/usr/bin/env bash
exit 0
SCRIPT
chmod +x "$test_dir/bin/sbatch" "$test_dir/bin/squeue" "$test_dir/bin/python3"
HOME="$test_dir/home" HPCLIB_DIR="$HPCLIB_DIR" \
  HPCLIB_TUNNEL_PATH="$HPCLIB_DIR/tunnels" HPCSESSIONS_DIR="$test_dir/sessions" \
  HPCSERVERS_DIR="$test_dir" TEST_SBATCH_ARGS_FILE="$test_dir/sbatch-args" \
  PATH="$test_dir/bin:$PATH" \
  bash "$HPCLIB_DIR/tunnels/start_tunnel.sh" flask -P 5050 --mem=2gb -- \
    'mypackage:create_app("hello world")' > "$test_dir/start-flask.log" 2>&1
assert_equal "$(tail -n 2 "$test_dir/sbatch-args" | head -n 1)" "$HPCLIB_DIR/tunnels/flask/sbatch_script.sh"
assert_equal "$(tail -n 1 "$test_dir/sbatch-args")" 'mypackage:create_app("hello world")'

# The Flask batch script uses the chosen compute-node port and app.
mkdir -p "$test_dir/common" "$test_dir/flask-bin"
touch "$test_dir/common/configure_job.sh"
cat > "$test_dir/flask-bin/flask" <<'SCRIPT'
#!/usr/bin/env bash
printf '%s\n' "$@" > "$TEST_FLASK_ARGS_FILE"
SCRIPT
chmod +x "$test_dir/flask-bin/flask"
(
  TUNNEL_DIR="$HPCLIB_DIR/tunnels/flask"
  HPCTUNNELS_DIR="$test_dir/common"
  PROCESS_PORT=6123
  TEST_FLASK_ARGS_FILE="$test_dir/flask-args"
  PATH="$test_dir/flask-bin:$PATH"
  export TUNNEL_DIR HPCTUNNELS_DIR PROCESS_PORT TEST_FLASK_ARGS_FILE PATH
  bash "$TUNNEL_DIR/sbatch_script.sh" 'mypackage.web:app' --no-threads
)
printf '%s\n' --app mypackage.web:app run --no-threads --host=127.0.0.1 \
  --port=6123 --no-reload --no-debugger > "$test_dir/expected-flask-args"
diff -u "$test_dir/expected-flask-args" "$test_dir/flask-args" || fail 'wrong Flask command'

# -A none skips the browser entirely
(
  HOME="$test_dir/home"
  _wait_for_port() { echo called > "$test_dir/browser-waited"; return 1; }
  pssh() { :; }
  launch_tunnel -A none -P 5050 login.example rest >/dev/null 2>&1
  wait
)
[ ! -e "$test_dir/browser-waited" ] || fail '-A none still tried to open a browser'

################################################################################
# Clearing up after tunnels. These need the real python3, not the stub the
# start_tunnel test above put on PATH.
CLEAN_PATH="${PATH//$test_dir\/bin:/}"

free_port() { PATH="$CLEAN_PATH" python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
# a process listening on PORT whose command line is NAME (what pkill -f sees)
listen_as() {  # listen_as PORT NAME...
  local port="$1"; shift
  (exec -a "$*" python3 -c 'import socket, sys, time
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen(); time.sleep(300)' "$port") &
  for _ in $(seq 50); do _hpclib_port_free "$port" || break; sleep 0.1; done
}
mkdir -p "$test_dir/clear-bin"
cat > "$test_dir/clear-bin/squeue" <<'SCRIPT'
#!/usr/bin/env bash
# answers from a script of responses, one line per call: "ID", "ended" or "down"
n=$(cat "$TEST_SQUEUE_STATE.n" 2>/dev/null || echo 0); echo $((n + 1)) > "$TEST_SQUEUE_STATE.n"
reply=$(sed -n "$((n + 1))p" "$TEST_SQUEUE_STATE"); [ -n "$reply" ] || reply=ended
case "$reply" in
  ended) echo "slurm_load_jobs error: Invalid job id specified" >&2; exit 1 ;;
  down) echo "slurm_load_jobs error: Unable to contact slurm controller" >&2; exit 1 ;;
  *) echo "$reply" ;;
esac
SCRIPT
cat > "$test_dir/clear-bin/scancel" <<'SCRIPT'
#!/usr/bin/env bash
echo "$@" >> "$TEST_SCANCEL_LOG"
SCRIPT
chmod +x "$test_dir/clear-bin/"*
(
  PATH="$test_dir/clear-bin:$CLEAN_PATH"
  HPCSESSIONS_DIR="$test_dir/clear-sessions"
  TEST_SQUEUE_STATE="$test_dir/squeue-replies"; TEST_SCANCEL_LOG="$test_dir/scancel.log"
  export PATH HPCSESSIONS_DIR TEST_SQUEUE_STATE TEST_SCANCEL_LOG

  # postconnect.sh follows the log while the job is queued (riding out a
  # controller hiccup), then returns and cancels the job
  printf '%s\n' 777 down 777 ended > "$TEST_SQUEUE_STATE"
  echo "server started" > "$test_dir/session.log"
  out=$(SESSION_FILE="$test_dir/session.log" SESSION_ID=777 TUNNEL_POLL_INTERVAL=0.1 \
        timeout 20 bash "$HPCLIB_DIR/tunnels/postconnect.sh") || fail "postconnect.sh did not return when the job ended"
  case "$out" in *"server started"*"job 777 has ended"*) ;; *) fail "postconnect output: $out" ;; esac
  assert_equal "$(cat "$TEST_SQUEUE_STATE.n")" 4
  assert_equal "$(cat "$TEST_SCANCEL_LOG")" 777
  if pgrep -f "tail -f -n \+1 $test_dir/session.log" >/dev/null; then fail 'postconnect.sh left tail running'; fi
  rm -f "$TEST_SQUEUE_STATE"* "$TEST_SCANCEL_LOG"

  # a leftover forward and waiting page on the port are stopped
  port=$(free_port)
  listen_as "$port" ssh -L "127.0.0.1:$port:127.0.0.1:5000" -t chem-node
  _hpclib_port_free "$port" && fail 'test listener did not start'
  _hpclib_clear_port "$port" 2> "$test_dir/clear.err" || fail "did not clear a stale forward: $(cat "$test_dir/clear.err")"
  grep -q 'leftover port forward' "$test_dir/clear.err" || fail 'no message about the stale forward'
  _hpclib_port_free "$port" || fail 'port still busy after clearing'

  port=$(free_port)
  listen_as "$port" python3 "$HPCLIB_DIR/servers/waiting_shim.py" "$port" /tmp/status rest
  _hpclib_clear_port "$port" 2>/dev/null || fail 'did not clear a stale waiting page'

  # an earlier tunnel script on the port is stopped and its job cancelled
  port=$(free_port)
  (exec -a "/bin/bash hpclib/tunnels/start_tunnel.sh rest -P $port" sleep 300) &
  old=$!
  _hpclib_record_port "$port" "$old" 4242
  printf '%s\n' 4242 > "$TEST_SQUEUE_STATE"
  _hpclib_clear_port "$port" 2> "$test_dir/clear.err" || fail 'did not clear an earlier tunnel'
  sleep 0.3
  if kill -0 "$old" 2>/dev/null; then fail 'earlier tunnel script still running'; fi
  assert_equal "$(cat "$TEST_SCANCEL_LOG")" 4242
  [ ! -e "$(_hpclib_port_file "$port")" ] || fail 'port record left behind'
  rm -f "$TEST_SQUEUE_STATE"* "$TEST_SCANCEL_LOG"

  # a dead PID in the record (or one reused by an unrelated process) is left alone
  port=$(free_port)
  sleep 300 & unrelated=$!
  _hpclib_record_port "$port" "$unrelated"
  _hpclib_clear_port "$port" 2>/dev/null || fail 'stale record blocked the port'
  kill -0 "$unrelated" 2>/dev/null || fail 'killed an unrelated process with a recycled PID'
  kill "$unrelated"

  # a port held by something else is an error, and that process is left alone
  port=$(free_port)
  listen_as "$port" python3 -m some_other_service
  holder=$!
  if _hpclib_clear_port "$port" 2> "$test_dir/clear.err"; then fail 'claimed a port held by another program'; fi
  grep -q 'pick another port' "$test_dir/clear.err" || fail 'no advice for a busy port'
  kill -0 "$holder" 2>/dev/null || fail 'killed a process that is not a tunnel'
  kill "$holder"

  # records are only forgotten by their owner
  _hpclib_record_port 1234 111 9
  _hpclib_forget_port 1234 222
  [ -e "$(_hpclib_port_file 1234)" ] || fail 'forgot a record that belongs to another tunnel'
  _hpclib_forget_port 1234 111
  [ ! -e "$(_hpclib_port_file 1234)" ] || fail 'did not forget its own record'
)

# the forward to the compute node fails instead of running without its port
(
  HOME="$test_dir/home"
  wait_for_job_node() { echo node7; }
  ssh() { printf '%s\n' "$@" > "$test_dir/connect-args"; }
  connect_to_job -P 5999:5000 -R 1 -S 0 -I 0 123 "echo hi" >/dev/null
)
grep -qx 'ExitOnForwardFailure=yes' "$test_dir/connect-args" || fail 'connect_to_job without ExitOnForwardFailure'
grep -qx 'ServerAliveInterval=30' "$test_dir/connect-args" || fail 'connect_to_job without keepalives'
grep -qx '127.0.0.1:5999:127.0.0.1:5000' "$test_dir/connect-args" || fail 'connect_to_job lost its forward'

# stop_tunnel clears the login node over ssh and cancels the local forward
mkdir -p "$test_dir/stop-bin"
cat > "$test_dir/stop-bin/ssh" <<'SCRIPT'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TEST_SSH_LOG"
after_host=false; remote=''
for a in "$@"; do
  if [ "$after_host" = true ]; then remote="$remote${remote:+ }$a"
  elif [ "$a" = login.example ]; then after_host=true; fi
done
case " $* " in *" -O cancel "*) exit 0 ;; esac
exec bash -c "$remote"
SCRIPT
chmod +x "$test_dir/stop-bin/ssh"
(
  HOME="$test_dir/home"; PATH="$test_dir/stop-bin:$test_dir/clear-bin:$CLEAN_PATH"
  HPCSESSIONS_DIR="$test_dir/stop-sessions"; TEST_SSH_LOG="$test_dir/stop-ssh.log"
  TEST_SQUEUE_STATE="$test_dir/none"; TEST_SCANCEL_LOG="$test_dir/stop-scancel.log"
  export HOME PATH HPCSESSIONS_DIR TEST_SSH_LOG TEST_SQUEUE_STATE TEST_SCANCEL_LOG
  port=$(free_port)
  listen_as "$port" ssh -L "127.0.0.1:$port:127.0.0.1:5000" -t chem-node
  out=$(stop_tunnel -P "$port" login.example 2>&1) || fail "stop_tunnel failed: $out"
  case "$out" in *"port $port is free"*) ;; *) fail "stop_tunnel output: $out" ;; esac
  _hpclib_port_free "$port" || fail 'stop_tunnel left the port busy'
  grep -q -- "-O cancel -L 127.0.0.1:$port:127.0.0.1:$port" "$TEST_SSH_LOG" || fail 'local forward not cancelled'
  grep -q 'ServerAliveInterval=30' "$TEST_SSH_LOG" || fail 'pssh without keepalives'
  if stop_tunnel login.example >/dev/null 2>&1; then fail 'stop_tunnel ran without -P'; fi
)

echo 'Tunnel management tests passed'
