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

# --login-node: no sbatch; the tunnel's script runs on the login node, on the forwarded port, until it ends.
mkdir -p "$test_dir/login-tunnels/svc"
cat > "$test_dir/login-tunnels/svc/sbatch_script.sh" <<'SCRIPT'
#!/usr/bin/env bash
echo "svc on port $PROCESS_PORT login=$TUNNEL_ON_LOGIN_NODE greeting=$GREETING args=$*"
SCRIPT
printf 'START_GIT_SERVER=false\nSTART_SLURM_SERVER=false\n' > "$test_dir/login-tunnels/svc/tunnel_config.sh"
rm -f "$test_dir/sbatch-args"
HOME="$test_dir/home" HPCLIB_DIR="$HPCLIB_DIR" \
  HPCLIB_TUNNEL_PATH="$test_dir/login-tunnels:$HPCLIB_DIR/tunnels" HPCSESSIONS_DIR="$test_dir/sessions" \
  HPCSERVERS_DIR="$test_dir" TEST_SBATCH_ARGS_FILE="$test_dir/sbatch-args" PATH="$test_dir/bin:$PATH" \
  timeout 60 bash "$HPCLIB_DIR/tunnels/start_tunnel.sh" svc -P 5151 --login-node --mem=2gb --env=GREETING=hi -- \
    one two > "$test_dir/login.log" 2>&1 || fail "the login-node tunnel failed: $(cat "$test_dir/login.log")"
[ ! -e "$test_dir/sbatch-args" ] || fail 'a login-node tunnel called sbatch'
grep -q 'svc on port 5151 login=true greeting=hi args=one two' "$test_dir/login.log" ||
  fail "the login-node script didn't run with the forwarded port: $(cat "$test_dir/login.log")"
grep -q 'ignoring the sbatch options: --mem=2gb' "$test_dir/login.log" || fail "sbatch options were silently dropped: $(cat "$test_dir/login.log")"
grep -q 'on the login node has ended (exit 0)' "$test_dir/login.log" || fail 'the end was not reported'
[ ! -e "$test_dir/sessions/ports/$(hostname -s)-5151" ] || fail 'the port record was left behind'
if HOME="$test_dir/home" HPCLIB_DIR="$HPCLIB_DIR" HPCLIB_TUNNEL_PATH="$HPCLIB_DIR/tunnels" \
  HPCSESSIONS_DIR="$test_dir/sessions" HPCSERVERS_DIR="$test_dir" PATH="$test_dir/bin:$PATH" \
  timeout 60 bash "$HPCLIB_DIR/tunnels/start_tunnel.sh" pai -P 5152 --login-node > "$test_dir/login-pai.log" 2>&1; then
  fail 'ran a shared-instance tunnel on the login node'
fi
grep -q "can't run on the login node" "$test_dir/login-pai.log" || fail 'no reason for refusing --login-node'

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
grep -q 'StrictHostKeyChecking' "$test_dir/connect-args" && fail 'accepted host keys without being asked to'
(
  HOME="$test_dir/home"; HPCLIB_COMPUTE_HOST_KEYS=accept-new
  wait_for_job_node() { echo node7; }
  ssh() { printf '%s\n' "$@" > "$test_dir/connect-args"; }
  connect_to_job -P 5999:5000 -R 1 -S 0 -I 0 123 "echo hi" >/dev/null
)
grep -qx 'StrictHostKeyChecking=accept-new' "$test_dir/connect-args" || fail 'HPCLIB_COMPUTE_HOST_KEYS=accept-new ignored'

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

# A tunnel job that dies before it gets a node is reported, not waited on forever
mkdir -p "$test_dir/dead-bin" "$test_dir/dead-sessions/rest"
cat > "$test_dir/dead-bin/sbatch" <<'SCRIPT'
#!/usr/bin/env bash
echo "(from job_submit) your job is charged as below" >&2   # like Grace's job_submit plugin
echo "conda: command not found" > "$TEST_DEAD_SESSIONS/rest/session-4242.log"
echo 4242
SCRIPT
cat > "$test_dir/dead-bin/squeue" <<'SCRIPT'
#!/usr/bin/env bash
case " $* " in *" -j 4242 "*) echo "slurm_load_jobs error: Invalid job id specified" >&2; exit 1 ;; esac
exit 0
SCRIPT
cat > "$test_dir/dead-bin/sacct" <<'SCRIPT'
#!/usr/bin/env bash
echo 'FAILED|127:0'
SCRIPT
printf '#!/usr/bin/env bash\nexit 0\n' > "$test_dir/dead-bin/scancel"
chmod +x "$test_dir/dead-bin/"*
if HOME="$test_dir/home" HPCLIB_DIR="$HPCLIB_DIR" HPCLIB_TUNNEL_PATH="$HPCLIB_DIR/tunnels" \
    HPCSESSIONS_DIR="$test_dir/dead-sessions" TEST_DEAD_SESSIONS="$test_dir/dead-sessions" \
    HPCSERVERS_DIR="$HPCLIB_DIR/servers" PATH="$test_dir/dead-bin:$PATH" \
    timeout 60 bash "$HPCLIB_DIR/tunnels/start_tunnel.sh" rest -P 23456 > "$test_dir/dead.log" 2>&1; then
  fail 'start_tunnel succeeded with a job that died'
fi
dead_out=$(cat "$test_dir/dead.log")
case "$dead_out" in *'Job 4242 ended before the tunnel connected (FAILED 127:0)'*) ;; *) fail "no report of the dead job: $dead_out" ;; esac
case "$dead_out" in *'conda: command not found'*) ;; *) fail "the job's log was not shown: $dead_out" ;; esac
case "$dead_out" in *'Submitted batch job 4242'*) ;; *) fail "the job id was not read from sbatch: $dead_out" ;; esac

# configure_job.sh: conda is used when there, loaded from CONDA_MODULE if asked, and otherwise skipped
mkdir -p "$test_dir/conda-home" "$test_dir/conda-bin"
: > "$test_dir/conda-home/.bashrc"
cat > "$test_dir/conda-bin/conda" <<'SCRIPT'
#!/usr/bin/env bash
# a conda from a module: its shell hook defines the conda function that can activate
[ "$1 $2" = "shell.bash hook" ] && echo 'conda() { echo "activated $2" >> "$TEST_CONDA_LOG"; }'
SCRIPT
chmod +x "$test_dir/conda-bin/conda"
run_configure() {  # run_configure ENV... : configure_job.sh under set -e, as the sbatch scripts source it
  env -i HOME="$test_dir/conda-home" PATH="$CLEAN_PATH" TEST_CONDA_LOG="$test_dir/conda.log" "$@" \
    bash -c "set -e; source '$HPCLIB_DIR/tunnels/configure_job.sh'; echo configured"
}
out=$(run_configure CONDA_ENVIRONMENT=default 2>&1) || fail "a missing conda stopped the job: $out"
case "$out" in *"conda isn't available"*configured*) ;; *) fail "no warning about the missing conda: $out" ;; esac
out=$(run_configure CONDA_ENVIRONMENT= 2>&1) || fail "configure_job failed without a conda environment: $out"
case "$out" in *conda*) fail "mentioned conda although no environment was asked for: $out" ;; esac
module_fn="() { [ \"\$1 \$2\" = \"load Anaconda3/2024.02\" ] && PATH=\"$test_dir/conda-bin:\$PATH\"; }"
out=$(run_configure CONDA_ENVIRONMENT=myenv CONDA_MODULE=Anaconda3/2024.02 "BASH_FUNC_module%%=$module_fn" 2>&1) ||
  fail "conda from a module failed: $out"
assert_equal "$(cat "$test_dir/conda.log")" 'activated myenv'
if run_configure CONDA_ENVIRONMENT=myenv REQUIRE_CONDA=true > /dev/null 2>&1; then
  fail 'REQUIRE_CONDA=true accepted a missing conda'
fi

################################################################################
# Shared instances (pai): a job registers its service and port; a tunnel attaches to a running one.
mkdir -p "$test_dir/share-bin" "$test_dir/share-home" "$test_dir/share-data"
: > "$test_dir/share-home/.bashrc"
printf 'JOB_INITIALIZATION_PAUSE=0\nJOB_CONNECT_RETRY_WAIT_TIME=0\n' > "$test_dir/share-data/config.sh"
cat > "$test_dir/share-bin/squeue" <<'SCRIPT'
#!/usr/bin/env bash
# jobs in $TEST_RUNNING run (on node9); others have ended
job=''; fmt=''
while [ "$#" -gt 0 ]; do case "$1" in -j) job="$2"; shift ;; -o|--format) fmt="$2"; shift ;; --format=*) fmt="${1#*=}" ;; esac; shift; done
case " $TEST_RUNNING " in
  *" $job "*)
    case "$fmt" in *%T*) out=RUNNING ;; *%N*) out=node9 ;; *%u*) out='' ;; *) out="$job" ;; esac
    case "$fmt" in *%u*) out="$out${out:+ }${TEST_OWNER:-$(id -un)}" ;; esac
    echo "$out" ;;
  *) [ -n "$job" ] && { echo "slurm_load_jobs error: Invalid job id specified" >&2; exit 1; } ;;
esac
exit 0
SCRIPT
cat > "$test_dir/share-bin/sbatch" <<'SCRIPT'
#!/usr/bin/env bash
echo submitted >> "$TEST_SHARE_LOG"; echo 5151
SCRIPT
cat > "$test_dir/share-bin/scancel" <<'SCRIPT'
#!/usr/bin/env bash
echo "scancel $*" >> "$TEST_SHARE_LOG"
SCRIPT
cat > "$test_dir/share-bin/ssh" <<'SCRIPT'
#!/usr/bin/env bash
echo "ssh $*" >> "$TEST_SHARE_LOG"
SCRIPT
chmod +x "$test_dir/share-bin/"*
share_env=(HOME="$test_dir/share-home" HPCLIB_DIR="$HPCLIB_DIR" HPCLIB_TUNNEL_PATH="$HPCLIB_DIR/tunnels"
           HPCTUNNELS_DATA_DIR="$test_dir/share-data" HPCSESSIONS_DIR="$test_dir/share-data/sessions"
           HPCSERVERS_DIR="$test_dir" TEST_SHARE_LOG="$test_dir/share.log" PATH="$test_dir/share-bin:$CLEAN_PATH")
(
  export "${share_env[@]}"
  source "$HPCLIB_DIR/tunnels/instances.sh"
  # a job registers itself; another finds it while it runs, and forgets it once it has ended
  SLURM_JOB_ID=4000 tunnel_register_instance pai 3100
  sleep 1
  SLURM_JOB_ID=4001 tunnel_register_instance pai 3999      # newer, and its job has ended
  assert_equal "$(tunnel_instance_port pai 4001)" 3999
  assert_equal "$(TEST_RUNNING=4000 tunnel_find_instance pai)" "4000 $(hostname -s) 3100"
  [ ! -e "$test_dir/share-data/instances/pai/4001" ] || fail 'kept the instance of an ended job'
  if TEST_RUNNING= tunnel_find_instance pai > /dev/null; then fail 'found an instance whose job ended'; fi
  # a taken port isn't picked
  port=$(free_port); listen_as "$port" python3 -m held
  picked=$(tunnel_pick_port "$port")
  [ -n "$picked" ] && [ "$picked" != "$port" ] || fail "picked a busy port ($picked)"
  assert_equal "$(tunnel_pick_port "$picked")" "$picked"
  kill %% 2>/dev/null || true
)
# start_tunnel attaches to the running instance: no new job, its port, and it isn't cancelled afterwards
(
  export "${share_env[@]}"
  SLURM_JOB_ID=6000 bash -c "source '$HPCLIB_DIR/tunnels/instances.sh'; tunnel_register_instance pai 3777"
  TEST_RUNNING=6000 timeout 60 bash "$HPCLIB_DIR/tunnels/start_tunnel.sh" pai -P "$(free_port)" > "$test_dir/attach.log" 2>&1 ||
    fail "attaching failed: $(cat "$test_dir/attach.log")"
)
grep -q 'attaching to the running pai instance: job 6000' "$test_dir/attach.log" || fail "no attach message: $(cat "$test_dir/attach.log")"
grep -q submitted "$test_dir/share.log" && fail 'submitted a job although one runs'
grep -q -- '127.0.0.1:[0-9]*:127.0.0.1:3777' "$test_dir/share.log" || fail "not forwarded to the instance's port: $(cat "$test_dir/share.log")"
grep -q scancel "$test_dir/share.log" && fail 'cancelled a job it only attached to'
rm -f "$test_dir/share.log" "$test_dir/share-data/instances/pai/"*
# with none running it submits a job and forwards to the port that job reports (PROCESS_PORT_FROM_JOB)
(
  export "${share_env[@]}"
  ( sleep 1; SLURM_JOB_ID=5151 bash -c "source '$HPCLIB_DIR/tunnels/instances.sh'; tunnel_register_instance pai 3888" ) &
  TEST_RUNNING=5151 timeout 60 bash "$HPCLIB_DIR/tunnels/start_tunnel.sh" pai -P "$(free_port)" > "$test_dir/own.log" 2>&1 ||
    fail "starting its own instance failed: $(cat "$test_dir/own.log")"
)
grep -q submitted "$test_dir/share.log" || fail 'no job submitted'
grep -q 'job 5151 serves pai on node9, port 3888' "$test_dir/own.log" || fail "port not taken from the job: $(cat "$test_dir/own.log")"
grep -q -- '127.0.0.1:[0-9]*:127.0.0.1:3888' "$test_dir/share.log" || fail "not forwarded to the reported port"
grep -q scancel "$test_dir/share.log" && fail 'cancelled the job pai keeps for others (KEEP_INSTANCE)'
# postconnect leaves an attached job alone
printf '%s\n' ended > "$test_dir/pc-replies"
(
  PATH="$test_dir/clear-bin:$CLEAN_PATH" TEST_SQUEUE_STATE="$test_dir/pc-replies" TEST_SCANCEL_LOG="$test_dir/pc-scancel.log" \
    SESSION_FILE="$test_dir/session.log" SESSION_ID=6000 TUNNEL_ATTACHED=true TUNNEL_POLL_INTERVAL=0.1 \
    timeout 20 bash "$HPCLIB_DIR/tunnels/postconnect.sh" > /dev/null
)
[ ! -e "$test_dir/pc-scancel.log" ] || fail 'postconnect cancelled an attached job'

################################################################################
# setup_tunnel.sh: settings, check, install (the console's Install and Check)
mkdir -p "$test_dir/setup-home"
: > "$test_dir/setup-home/.bashrc"
setup() {
  env HOME="$test_dir/setup-home" HPCLIB_DIR="$HPCLIB_DIR" HPCLIB_TUNNEL_PATH="$HPCLIB_DIR/tunnels" \
    HPCTUNNELS_DATA_DIR="$test_dir/setup-data" PATH="$test_dir/bin:$CLEAN_PATH" \
    bash "$HPCLIB_DIR/tunnels/setup_tunnel.sh" "$@"
}
image="$test_dir/my images/code server.sif"
out=$(setup vscode --set "VSCODE_CONTAINER=$image" --save --check)
case "$out" in *"HPCLIB_TUNNEL_STATUS missing not installed: no image at $image"*) ;; *) fail "check before install: $out" ;; esac
grep -q 'VSCODE_CONTAINER=' "$test_dir/setup-data/settings/vscode.sh" || fail 'settings not saved'
out=$(setup vscode --install --check) || fail "install failed: $out"
[ -f "$image" ] || fail 'the image was not pulled to the saved path'
case "$out" in *"HPCLIB_TUNNEL_STATUS installed installed: $image"*) ;; *) fail "check after install: $out" ;; esac
if setup vscode --set PATH=/evil --save > /dev/null 2>&1; then fail 'accepted a setting the tunnel does not have'; fi
grep -q "VSCODE_CONTAINER=" "$test_dir/setup-data/settings/vscode.sh" || fail 'a refused setting replaced the saved ones'
case "$(setup flask --check)" in *"HPCLIB_TUNNEL_STATUS nothing"*) ;; *) fail 'flask has nothing to install' ;; esac
case "$(setup pai --set "PAI_ROOT_DIR=$test_dir/pai" --check)" in *"HPCLIB_TUNNEL_STATUS missing"*) ;; *) fail 'pai check' ;; esac
# start_tunnel reads the saved settings
grep -q 'TUNNEL_SETTINGS_FILE="$HPCTUNNELS_DATA_DIR/settings/$TUNNEL_NAME.sh"' "$HPCLIB_DIR/tunnels/start_tunnel.sh" ||
  fail 'start_tunnel does not read the settings file'
# tunnel_setup runs it on the cluster with the options quoted
(
  pssh() { printf '%s\n' "$@" > "$test_dir/setup-remote"; }
  tunnel_setup -p 2222 me@login.example vscode --set "VSCODE_CONTAINER=/a b/c.sif" --save --check
)
assert_equal "$(sed -n 1,3p "$test_dir/setup-remote" | tr '\n' ' ')" '-p 2222 me@login.example '
grep -q 'tunnels/setup_tunnel.sh vscode --set VSCODE_CONTAINER=/a\\ b/c.sif --save --check' "$test_dir/setup-remote" ||
  fail "remote command: $(cat "$test_dir/setup-remote")"

# --push DIR: the directory goes along as a tar on the same ssh call, to setup_tunnel.sh --receive
mkdir -p "$test_dir/push/settings.d" && printf 'x\n' > "$test_dir/push/settings.d/a.conf"
(
  pssh() { printf '%s\n' "$@" > "$test_dir/push-remote"; cat > "$test_dir/push-stdin"; }
  tunnel_setup me@login.example data-transfer --push "$test_dir/push" --set SMB_HOST=h --save --check
)
grep -q 'tunnels/setup_tunnel.sh data-transfer --receive --set SMB_HOST=h --save --check' "$test_dir/push-remote" ||
  fail "push remote command: $(cat "$test_dir/push-remote")"
tar -t -f "$test_dir/push-stdin" | grep -q 'settings.d/a.conf' || fail 'the pushed directory was not sent'
if tunnel_setup me@login.example vscode --push "$test_dir/nowhere" --check 2>/dev/null; then fail 'pushed a missing directory'; fi
# macOS's tar (bsdtar) leaves out extended attributes, which the cluster's GNU tar warns about
if command -v bsdtar > /dev/null 2>&1 && command -v setfattr > /dev/null 2>&1 &&
    setfattr -n user.com.apple.provenance -v 01 "$test_dir/push/settings.d/a.conf" 2> /dev/null; then
  mkdir -p "$test_dir/bsd-bin" && ln -sf "$(command -v bsdtar)" "$test_dir/bsd-bin/tar"
  (
    PATH="$test_dir/bsd-bin:$PATH"
    pssh() { cat > "$test_dir/push-stdin"; }
    tunnel_setup me@login.example data-transfer --push "$test_dir/push" --check
  )
  mkdir -p "$test_dir/push-out"
  warnings=$(tar -x -C "$test_dir/push-out" -f "$test_dir/push-stdin" 2>&1)
  [ -z "$warnings" ] || fail "the pushed archive makes GNU tar warn: $warnings"
fi

# setup_tunnel.sh lists a tunnel's running instances and ends one you own (PAI's "Stop database job")
(
  export "${share_env[@]}"
  stop() { bash "$HPCLIB_DIR/tunnels/setup_tunnel.sh" pai "$@"; }
  SLURM_JOB_ID=7100 bash -c "source '$HPCLIB_DIR/tunnels/instances.sh'; tunnel_register_instance pai 3101"
  SLURM_JOB_ID=7200 bash -c "source '$HPCLIB_DIR/tunnels/instances.sh'; tunnel_register_instance pai 3102"
  out=$(TEST_RUNNING=7100 stop --instances)
  assert_equal "$out" "HPCLIB_TUNNEL_INSTANCE 7100 $(hostname -s) 3101 RUNNING $(id -un)"
  [ ! -e "$test_dir/share-data/instances/pai/7200" ] || fail 'listed (kept) an ended instance'
  if TEST_RUNNING=7100 stop --stop-instance 9999 2>/dev/null; then fail 'cancelled a job that is not a pai instance'; fi
  if TEST_RUNNING=7100 TEST_OWNER=someone stop --stop-instance 7100 2>/dev/null; then fail "cancelled someone else's job"; fi
  grep -q 'scancel' "$TEST_SHARE_LOG" 2>/dev/null && fail 'scancel ran for a refused stop'
  assert_equal "$(TEST_RUNNING=7100 stop --stop-instance 7100)" 'HPCLIB_TUNNEL_INSTANCE_STOPPED 7100'
  grep -q 'scancel 7100' "$TEST_SHARE_LOG" || fail 'the job was not cancelled'
  [ ! -e "$test_dir/share-data/instances/pai/7100" ] || fail 'the stopped instance is still registered'
  SLURM_JOB_ID=7300 bash -c "source '$HPCLIB_DIR/tunnels/instances.sh'; tunnel_register_instance pai 3103"
  assert_equal "$(TEST_RUNNING= stop --stop-instance 7300)" 'HPCLIB_TUNNEL_INSTANCE_GONE 7300'
)

# the PAI job passes PAI_BIND_SOURCE (default 1) to singularity-compose.sh, and registers its port
mkdir -p "$test_dir/pai-common" "$test_dir/pai-root/proto-auto-interface"
printf '. %q\n' "$HPCLIB_DIR/tunnels/instances.sh" > "$test_dir/pai-common/configure_job.sh"
printf 'echo "bind=$PAI_BIND_SOURCE port=$PAI_PORT"\n' > "$test_dir/pai-root/proto-auto-interface/singularity-compose.sh"
pai_job() {
  env TUNNEL_DIR="$HPCLIB_DIR/tunnels/pai" HPCTUNNELS_DIR="$test_dir/pai-common" PAI_ROOT_DIR="$test_dir/pai-root" \
    HPCTUNNELS_DATA_DIR="$test_dir/pai-data" SLURM_JOB_ID=8100 PROCESS_PORT="$(free_port)" PATH="$CLEAN_PATH" "$@" \
    bash "$HPCLIB_DIR/tunnels/pai/sbatch_script.sh"
}
case "$(pai_job)" in *"bind=1 port="[0-9]*) ;; *) fail "PAI_BIND_SOURCE did not default to 1: $(pai_job)" ;; esac
case "$(pai_job PAI_BIND_SOURCE=0)" in *"from the images"*"bind=0 "*) ;; *) fail "PAI_BIND_SOURCE=0 not passed on" ;; esac
[ ! -e "$test_dir/pai-data/instances/pai/8100" ] || fail 'the PAI job left its registration behind'

# launch-tunnel-manager finds its hpclib through links (relative ones too) and starts the console with its page
mkdir -p "$test_dir/launch/bin" "$test_dir/launch/elsewhere"
printf '#!/bin/sh\ncase "$1" in -c) exit 0 ;; esac\nprintf "%%s\\n" "$@"\n' > "$test_dir/launch/fake-python"
chmod +x "$test_dir/launch/fake-python"
ln -s "$HPCLIB_DIR/launch-tunnel-manager" "$test_dir/launch/elsewhere/ltm"
ln -s ../elsewhere/ltm "$test_dir/launch/bin/launch-tunnel-manager"            # a link to a link
launched=$(cd / && HPCLIB_PYTHON="$test_dir/launch/fake-python" "$test_dir/launch/bin/launch-tunnel-manager" --port 27999)
repo_root="$(cd -P "$HPCLIB_DIR/.." && pwd)"
assert_equal "$(printf '%s\n' "$launched" | head -n 1)" "$(cd -P "$HPCLIB_DIR" && pwd)/servers/agent_console.py"
assert_equal "$(printf '%s\n' "$launched" | tail -n +2 | tr '\n' ' ')" "--static $repo_root/agent-console --port 27999 --open "
launched=$(HPCLIB_PYTHON="$test_dir/launch/fake-python" "$test_dir/launch/bin/launch-tunnel-manager" --no-open)
case "$launched" in *--open*) fail '--no-open still opened the browser' ;; esac
case "$("$test_dir/launch/bin/launch-tunnel-manager" --where)" in
  *"hpclib:   $(cd -P "$HPCLIB_DIR" && pwd) ("*"web page: $repo_root/agent-console"*) ;;
  *) fail "--where: $("$test_dir/launch/bin/launch-tunnel-manager" --where)" ;;
esac
out=$(PATH="/usr/bin:/bin" "$HPCLIB_DIR/launch-tunnel-manager" --install-link "$test_dir/launch/links")
[ "$(readlink "$test_dir/launch/links/launch-tunnel-manager")" = "$(cd -P "$HPCLIB_DIR" && pwd)/launch-tunnel-manager" ] ||
  fail "--install-link made $(readlink "$test_dir/launch/links/launch-tunnel-manager")"
case "$out" in *"isn't on your PATH"*) ;; *) fail "no PATH advice: $out" ;; esac
if HPCLIB_PYTHON=false "$HPCLIB_DIR/launch-tunnel-manager" > /dev/null 2>&1; then fail 'ran without a usable Python'; fi

################################################################################
# smbshell: rclone in the data-transfer-tools image, with Kerberos or a saved password
sb="$test_dir/smb"
mkdir -p "$sb/bin" "$sb/home" "$sb/data" "$sb/scratch/out" "$sb/images"
: > "$sb/images/dtt.sif"
cat > "$sb/bin/singularity" <<'SCRIPT'
#!/usr/bin/env bash
# runs the command directly, after noting the binds and the environment rclone gets
[ "$1" = exec ] || [ "$1" = shell ] || exit 2
shift; binds=()
while [ "$#" -gt 0 ]; do case "$1" in --bind) binds+=("$2"); shift 2 ;; -*) shift ;; *) break ;; esac; done
shift   # the image
printf 'binds=%s\n' "${binds[*]}" >> "$TEST_SMB_LOG"
exec "$@"
SCRIPT
cat > "$sb/bin/rclone" <<'SCRIPT'
#!/usr/bin/env bash
case "$1 $2" in
  "obscure -") printf 'OBS:%s\n' "$(cat)"; exit 0 ;;
  "help backend") echo "  --smb-use-kerberos  Use Kerberos authentication"; exit 0 ;;
  "version ") echo "rclone v1.70.0"; exit 0 ;;
esac
{ printf 'rclone'; printf ' %s' "$@"; printf '\n'
  env | grep -E '^(RCLONE_CONFIG_SMB_|KRB5CCNAME=|KRB5_CONFIG=)' | sort; } >> "$TEST_SMB_LOG"
if [ "$1" = rcd ]; then   # the web GUI: note its config, then stop as if Stop was pressed
  conf=''; prev=''; for a in "$@"; do [ "$prev" = --config ] && conf="$a"; prev="$a"; done
  { echo "--- config"; cat "$conf"; echo "--- end"; } >> "$TEST_SMB_LOG"; exit 0
fi
if [ -n "$TEST_RCLONE_REFUSE" ] && [ "$RCLONE_CONFIG_SMB_PASS" = "$TEST_RCLONE_REFUSE" ]; then
  echo "CRITICAL: couldn't connect SMB: response error: The attempted logon is invalid." >&2; exit 1
fi
SCRIPT
cat > "$sb/bin/kinit" <<'SCRIPT'
#!/usr/bin/env bash
echo "kinit $*" >> "$TEST_SMB_LOG"; : > "${KRB5CCNAME#FILE:}"
SCRIPT
cat > "$sb/bin/klist" <<'SCRIPT'
#!/usr/bin/env bash
[ -f "${KRB5CCNAME#FILE:}" ] || exit 1
[ "$1" = -s ] && exit 0
echo "Default principal: me@AUTH.EXAMPLE.EDU"
echo "10/05/2026 08:00:00  10/05/2026 18:00:00  krbtgt/AUTH.EXAMPLE.EDU@AUTH.EXAMPLE.EDU"
SCRIPT
printf '#!/usr/bin/env bash\nrm -f "${KRB5CCNAME#FILE:}"\n' > "$sb/bin/kdestroy"
cat > "$sb/bin/sbatch" <<'SCRIPT'
#!/usr/bin/env bash
{ printf 'sbatch'; printf ' %s' "$@"; printf '\nSMBSHELL_DIR=%s\n' "$SMBSHELL_DIR"; } >> "$TEST_SMB_LOG"; echo 9001
SCRIPT
chmod +x "$sb/bin/"*
mkdir -p "$sb/data/settings"
printf 'export SMB_HOST=files.example.edu SMB_DOMAIN=EXAMPLE SMB_REALM=AUTH.EXAMPLE.EDU SMB_IMAGE=%q\n' "$sb/images/dtt.sif" \
  > "$sb/data/settings/data-transfer.sh"
smbsh() {  # smbsh ARGS: smbshell on "the cluster", with no terminal
  env HOME="$sb/home" HPCTUNNELS_DATA_DIR="$sb/data" PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" \
    SMB_KRB5_CONF=/dev/null bash "$HPCLIB_DIR/tunnels/data-transfer/smbshell.sh" "$@" < /dev/null
}
case "$(smbsh status --json)" in
  *'"host": "files.example.edu"'*'"kinit": "host"'*'"rclone_kerberos": true'*'"ticket": null'*'"credentials": false'*) ;;
  *) fail "status: $(smbsh status --json)" ;;
esac
if smbsh ls proj 2> "$sb/err"; then fail 'ls signed in with nothing to sign in with'; fi
grep -q 'smbshell login (Kerberos) or smbshell save-credentials' "$sb/err" || fail "no advice: $(cat "$sb/err")"
if smbsh submit get proj/raw "$sb/scratch/out" 2> "$sb/err"; then fail 'submitted a job that could not sign in'; fi

# a saved password (as save-credentials leaves it), given to rclone in its environment only
mkdir -p "$sb/home/.config/hpclib/smb" && printf 'pass=OBS:hunter2\n' > "$sb/home/.config/hpclib/smb/credentials"
: > "$sb/log"; smbsh ls proj/raw --json
grep -q '^rclone lsjson --retries 1 --low-level-retries 1 smb:proj/raw$' "$sb/log" || fail "ls: $(cat "$sb/log")"
grep -q '^RCLONE_CONFIG_SMB_PASS=OBS:hunter2$' "$sb/log" || fail 'the saved password was not used'
grep -q '^RCLONE_CONFIG_SMB_HOST=files.example.edu$' "$sb/log" && grep -q '^RCLONE_CONFIG_SMB_DOMAIN=EXAMPLE$' "$sb/log" ||
  fail 'host or domain missing'
# paths under SMB_ROOT, a share and folder: relative ones there, /SHARE/... from the top
: > "$sb/log"; SMB_ROOT=research/our_group smbsh ls raw
grep -q '^rclone lsf --retries 1 --low-level-retries 1 smb:research/our_group/raw$' "$sb/log" || fail "root: $(cat "$sb/log")"
: > "$sb/log"; SMB_ROOT=research/our_group smbsh ls
grep -q '^rclone lsf --retries 1 --low-level-retries 1 smb:research/our_group$' "$sb/log" || fail "root itself: $(cat "$sb/log")"
: > "$sb/log"; SMB_ROOT=research/our_group smbsh ls /other/x
grep -q '^rclone lsf --retries 1 --low-level-retries 1 smb:other/x$' "$sb/log" || fail "absolute: $(cat "$sb/log")"
: > "$sb/log"; smbsh ls
grep -q '^rclone lsf --retries 1 --low-level-retries 1 smb:$' "$sb/log" || fail "the shares: $(cat "$sb/log")"
smbsh forget-credentials > /dev/null
[ ! -e "$sb/home/.config/hpclib/smb/credentials" ] || fail 'forget-credentials kept the password'

# save-credentials asks at a terminal and writes the encoded password, mode 600
HOME="$sb/home" HPCTUNNELS_DATA_DIR="$sb/data" PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" SMB_KRB5_CONF=/dev/null \
  python3 - "$HPCLIB_DIR/tunnels/data-transfer/smbshell.sh" <<'PY'
import os, pty, sys, time
pid, fd = pty.fork()
if pid == 0:
    os.execvp("bash", ["bash", sys.argv[1], "save-credentials"])
out = b""
while b"Password for" not in out:
    out += os.read(fd, 1024)
os.write(fd, b"s3cret\n")
while True:
    try:
        chunk = os.read(fd, 1024)
    except OSError:
        break
    if not chunk:
        break
    out += chunk
os.waitpid(pid, 0)
assert b"s3cret" not in out, out          # not echoed
PY
assert_equal "$(cat "$sb/home/.config/hpclib/smb/credentials")" 'pass=OBS:s3cret'
assert_equal "$(stat -c %a "$sb/home/.config/hpclib/smb/credentials" 2>/dev/null || stat -f %Lp "$sb/home/.config/hpclib/smb/credentials")" 600
rm -f "$sb/home/.config/hpclib/smb/credentials"

# Kerberos: login keeps the ticket where jobs see it; transfers use it, and no password
smbsh login > "$sb/out" || fail "login: $(cat "$sb/out")"
grep -q '^kinit me@AUTH.EXAMPLE.EDU$' "$sb/log" 2>/dev/null || grep -q "^kinit $(id -un)@AUTH.EXAMPLE.EDU$" "$sb/log" ||
  fail "kinit: $(cat "$sb/log")"
grep -q 'signed in: me@AUTH.EXAMPLE.EDU' "$sb/out" || fail "login output: $(cat "$sb/out")"
[ -f "$sb/home/.config/hpclib/smb/krb5cc" ] || fail 'the ticket cache is not where jobs look'
: > "$sb/log"; smbsh get //other.example.edu/proj/raw "$sb/scratch/out" --include '*.h5'
grep -q "^rclone copy --stats=1m --stats-one-line -v smb:proj/raw $sb/scratch/out --include \*.h5$" "$sb/log" ||
  fail "get: $(cat "$sb/log")"
grep -q '^RCLONE_CONFIG_SMB_USE_KERBEROS=true$' "$sb/log" || fail 'Kerberos not used'
grep -q '^RCLONE_CONFIG_SMB_HOST=other.example.edu$' "$sb/log" || fail '//HOST/... did not pick the host'
grep -q 'RCLONE_CONFIG_SMB_PASS' "$sb/log" && fail 'a password went with a Kerberos transfer'
grep -q "binds=.*$sb/scratch/out" "$sb/log" || fail 'the local folder was not bound into the image'
: > "$sb/log"; smbsh sync push "$sb/scratch/out" proj/out --dry-run 2> "$sb/err"
grep -q "^rclone sync --stats=1m --stats-one-line -v $sb/scratch/out smb:proj/out --dry-run$" "$sb/log" || fail "sync: $(cat "$sb/log")"
grep -q 'deleting what' "$sb/err" || fail 'sync did not warn that it deletes'

# submit: a job with the transfer, the tunnel's sbatch defaults, and yours
: > "$sb/log"; assert_equal "$(smbsh submit --time=1:00:00 get proj/raw "$sb/scratch/out" 2>/dev/null)" 9001
grep -q -- "--job-name=smb-transfer --output=$sb/data/sessions/data-transfer/transfer-%j.log --time=0-4:00:00 --mem=2gb --ntasks=1 --cpus-per-task=2 --time=1:00:00 $HPCLIB_DIR/tunnels/data-transfer/sbatch_script.sh get proj/raw $sb/scratch/out$" "$sb/log" ||
  fail "sbatch: $(cat "$sb/log")"
grep -q "^SMBSHELL_DIR=$HPCLIB_DIR/tunnels/data-transfer$" "$sb/log" || fail 'the job does not know where smbshell is'
printf '[["get", "proj/a", "%s/scratch/a"], ["put", "%s/scratch/out", "proj/b", "--checksum"]]\n' "$sb" "$sb" > "$sb/manifest.json"
: > "$sb/log"; smbsh submit --manifest "$sb/manifest.json" > /dev/null 2>&1 || fail 'manifest not submitted'
grep -q -- '--array=0-1 ' "$sb/log" || fail "no array: $(cat "$sb/log")"
lines=$(ls "$sb/data/sessions/data-transfer/"manifest-*.jsonl)
printf '[["ls"]]\n' > "$sb/bad.json"
if smbsh submit --manifest "$sb/bad.json" 2>/dev/null; then fail 'accepted a manifest entry that is not a transfer'; fi
# the job itself: entry 1 of the manifest, signed in without asking
: > "$sb/log"
env HOME="$sb/home" HPCTUNNELS_DATA_DIR="$sb/data" PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" SMB_KRB5_CONF=/dev/null \
  SMBSHELL_DIR="$HPCLIB_DIR/tunnels/data-transfer" SLURM_ARRAY_TASK_ID=1 SLURM_JOB_ID=9001 \
  bash "$HPCLIB_DIR/tunnels/data-transfer/sbatch_script.sh" --manifest "$lines" > "$sb/job.out" 2>&1 < /dev/null ||
  fail "the job failed: $(cat "$sb/job.out")"
grep -q "^rclone copy --stats=1m --stats-one-line -v $sb/scratch/out smb:proj/b --checksum$" "$sb/log" || fail "job: $(cat "$sb/log")"
# rclone's Kerberos library gets a krb5.conf it can read (RHEL's has includedir and dns_canonicalize_hostname=fallback)
printf 'includedir /etc/krb5.conf.d/\n[libdefaults]\n    dns_canonicalize_hostname = fallback\n    default_realm = AUTH.EXAMPLE.EDU\n' > "$sb/krb5.conf"
: > "$sb/log"
env HOME="$sb/home" HPCTUNNELS_DATA_DIR="$sb/data" PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" \
  SMB_KRB5_CONF="$sb/krb5.conf" bash "$HPCLIB_DIR/tunnels/data-transfer/smbshell.sh" ls proj < /dev/null
copy="$sb/home/.config/hpclib/smb/krb5.conf"
grep -q includedir "$copy" && fail 'includedir left in the copy for rclone'
grep -q '^    dns_canonicalize_hostname = false$' "$copy" || fail "fallback not turned into false: $(cat "$copy")"
grep -q 'default_realm = AUTH.EXAMPLE.EDU' "$copy" || fail 'the rest of krb5.conf was lost'
grep -q '^    dns_lookup_kdc = true$' "$copy" || fail "KDCs not looked up in DNS: $(cat "$copy")"
printf '[libdefaults]\n dns_lookup_kdc = false\n' > "$sb/krb5-nodns.conf"
env HOME="$sb/home" HPCTUNNELS_DATA_DIR="$sb/data" PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" \
  SMB_KRB5_CONF="$sb/krb5-nodns.conf" bash "$HPCLIB_DIR/tunnels/data-transfer/smbshell.sh" status > /dev/null < /dev/null
assert_equal "$(grep -c dns_lookup_kdc "$copy")" 1          # a site's own choice is kept
grep -q "binds=.*$copy:/etc/hpclib-krb5.conf:ro" "$sb/log" || fail 'the copy is not what the image sees'
grep -q '^RCLONE_CONFIG_SMB_USE_KERBEROS=true$' "$sb/log" || fail 'Kerberos not used with a server name'
# a server given by address: no Kerberos (no ticket names it), so the password
printf 'pass=OBS:hunter2\n' > "$sb/home/.config/hpclib/smb/credentials"
: > "$sb/log"; smbsh ls //10.0.0.12/proj 2> "$sb/err"
grep -q 'USE_KERBEROS' "$sb/log" && fail 'Kerberos used with an address'
grep -q '^RCLONE_CONFIG_SMB_PASS=OBS:hunter2$' "$sb/log" || fail 'the password was not used for an address'
grep -q "needs the server's name" "$sb/err" || fail "no note about the address: $(cat "$sb/err")"
if SMB_AUTH=kerberos smbsh ls //10.0.0.12/proj 2>/dev/null; then fail 'SMB_AUTH=kerberos accepted an address'; fi
# a user name as smbclient takes it, me@tamu.edu: rclone gets the user and the domain apart
mkdir -p "$sb/data2/settings"
printf 'export SMB_HOST=files.example.edu SMB_USER=me@tamu.edu SMB_IMAGE=%q\n' "$sb/images/dtt.sif" > "$sb/data2/settings/data-transfer.sh"
: > "$sb/log"
env HOME="$sb/home" HPCTUNNELS_DATA_DIR="$sb/data2" PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" SMB_AUTH=password \
  bash "$HPCLIB_DIR/tunnels/data-transfer/smbshell.sh" ls proj < /dev/null 2>/dev/null
grep -q '^RCLONE_CONFIG_SMB_USER=me$' "$sb/log" && grep -q '^RCLONE_CONFIG_SMB_DOMAIN=tamu.edu$' "$sb/log" ||
  fail "user@domain: $(cat "$sb/log")"
# and kinit asks for the user in the Kerberos realm, not in the Windows domain
: > "$sb/log"
env HOME="$sb/home" HPCTUNNELS_DATA_DIR="$sb/data2" PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" \
  bash "$HPCLIB_DIR/tunnels/data-transfer/smbshell.sh" login < /dev/null > /dev/null 2>&1
grep -qx 'kinit me' "$sb/log" || fail "kinit with user@domain: $(cat "$sb/log")"
: > "$sb/log"
env HOME="$sb/home" HPCTUNNELS_DATA_DIR="$sb/data2" PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" SMB_REALM=AUTH.TAMU.EDU \
  bash "$HPCLIB_DIR/tunnels/data-transfer/smbshell.sh" login < /dev/null > /dev/null 2>&1
grep -qx 'kinit me@AUTH.TAMU.EDU' "$sb/log" || fail "kinit with a realm: $(cat "$sb/log")"
rm -f "$sb/home/.config/hpclib/smb/krb5cc"
# find-spn: the names DNS gives for the server, tried with kvno; SMB_SPN then reaches rclone
cat > "$sb/bin/kvno" <<'SCRIPT'
#!/usr/bin/env bash
[ "$1" = "cifs/FILES" ]
SCRIPT
chmod +x "$sb/bin/kvno"
if smbsh find-spn > /dev/null 2>&1; then fail 'find-spn ran without a ticket'; fi
smbsh login > /dev/null
out=$(smbsh find-spn) || fail "find-spn: $out"
case "$out" in *"not known: cifs/files.example.edu"*"known:     cifs/FILES"*"--set SMB_SPN=cifs/FILES"*) ;; *) fail "find-spn: $out" ;; esac
: > "$sb/log"; SMB_SPN=cifs/FILES smbsh ls proj
grep -q '^RCLONE_CONFIG_SMB_SPN=cifs/FILES$' "$sb/log" || fail "SMB_SPN not given to rclone: $(cat "$sb/log")"
smbsh logout > /dev/null
# gui: rclone's web GUI for the session, its remotes in a private file that goes away with it
mkdir -p "$sb/run" "$sb/data/settings/data-transfer.d"
printf '[ours]\ntype = alias\nremote = smb:research/our_group\n' > "$sb/data/settings/data-transfer.d/rclone.conf"
printf 'pass=OBS:hunter2\n' > "$sb/home/.config/hpclib/smb/credentials"
: > "$sb/log"
out=$(env HOME="$sb/home" HPCTUNNELS_DATA_DIR="$sb/data" PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" \
  XDG_RUNTIME_DIR="$sb/run" SMB_ROOT=research/our_group SMB_AUTH=password \
  bash "$HPCLIB_DIR/tunnels/data-transfer/smbshell.sh" gui --port 27555 2>/dev/null < /dev/null) || fail "gui: $out"
case "$out" in "HPCLIB_RCLONE_GUI port=27555 user=hpclib pass="[0-9a-f]*) ;; *) fail "gui login line: $out" ;; esac
grep -q -- '--rc-web-gui --rc-web-gui-no-open-browser --rc-addr 127.0.0.1:27555 --rc-user hpclib --rc-pass ' "$sb/log" ||
  fail "rcd: $(cat "$sb/log")"
sed -n '/^--- config$/,/^--- end$/p' "$sb/log" > "$sb/gui.conf"
for want in '[smb]' 'type = smb' 'host = files.example.edu' 'domain = EXAMPLE' 'pass = OBS:hunter2' \
            '[cluster]' 'type = local' '[ours]' 'remote = smb:research/our_group'; do
  grep -qxF "$want" "$sb/gui.conf" || fail "gui config lacks '$want': $(cat "$sb/gui.conf")"
done
grep -qF '[lab]' "$sb/gui.conf" && fail 'the gui config still has a [lab] remote of its own'
# a settings file may add remotes, not replace smbshell's own
printf '[smb]\ntype = local\n' > "$sb/data/settings/data-transfer.d/rclone.conf"
if env HOME="$sb/home" HPCTUNNELS_DATA_DIR="$sb/data" PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" \
  XDG_RUNTIME_DIR="$sb/run" SMB_AUTH=password bash "$HPCLIB_DIR/tunnels/data-transfer/smbshell.sh" gui --port 27555 \
  > /dev/null 2>&1 < /dev/null; then fail 'a settings file replaced the smb remote'; fi
rm -rf "$sb/data/settings/data-transfer.d"
[ -z "$(ls -A "$sb/run")" ] || fail 'the session config was left behind'
if smbsh gui 2>/dev/null; then fail 'gui ran without a port'; fi
rm -f "$sb/home/.config/hpclib/smb/credentials"

# a refused sign-in: tried once, without rclone's retries, and the transfer isn't started
printf 'pass=OBS:wrong\n' > "$sb/home/.config/hpclib/smb/credentials"
smbsh logout > /dev/null
: > "$sb/log"
if TEST_RCLONE_REFUSE=OBS:wrong smbsh get //10.0.0.12/proj/raw "$sb/scratch/out" 2> "$sb/err"; then fail 'a refused sign-in went on'; fi
grep -q 'refused the sign-in (tried once' "$sb/err" || fail "refusal: $(cat "$sb/err")"
grep -q '^rclone lsf --max-depth 1 --retries 1 --low-level-retries 1 smb:proj$' "$sb/log" || fail "probe: $(cat "$sb/log")"
grep -q '^rclone copy' "$sb/log" && fail 'the transfer ran after a refused sign-in'
rm -f "$sb/home/.config/hpclib/smb/credentials"
smbsh logout > /dev/null
[ ! -e "$sb/home/.config/hpclib/smb/krb5cc" ] || fail 'logout kept the ticket'

# the image: checked and installed with setup_tunnel.sh, from a source of your choosing
rm -f "$sb/images/dtt.sif"
out=$(env HOME="$sb/home" HPCLIB_DIR="$HPCLIB_DIR" HPCLIB_TUNNEL_PATH="$HPCLIB_DIR/tunnels" HPCTUNNELS_DATA_DIR="$sb/data" \
  PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" bash "$HPCLIB_DIR/tunnels/setup_tunnel.sh" data-transfer \
  --set SMB_HOST=files.example.edu --set "SMB_IMAGE=$sb/images/dtt.sif" --set "SMB_IMAGE_SOURCE=$sb/built.sif" --save --check)
case "$out" in *"HPCLIB_TUNNEL_STATUS missing not installed: no image at $sb/images/dtt.sif"*) ;; *) fail "check: $out" ;; esac
: > "$sb/built.sif"
out=$(env HOME="$sb/home" HPCLIB_DIR="$HPCLIB_DIR" HPCLIB_TUNNEL_PATH="$HPCLIB_DIR/tunnels" HPCTUNNELS_DATA_DIR="$sb/data" \
  PATH="$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" bash "$HPCLIB_DIR/tunnels/setup_tunnel.sh" data-transfer --install --check)
case "$out" in *"HPCLIB_TUNNEL_STATUS installed installed: $sb/images/dtt.sif (rclone v1.70.0; Kerberos in rclone: yes)"*) ;;
  *) fail "install: $out" ;; esac
# by default it is pulled from the lab's registry, and Reinstall pulls it again
mkdir -p "$sb/pull-bin"
cat > "$sb/pull-bin/singularity" <<'SCRIPT'
#!/usr/bin/env bash
if [ "$1" = pull ]; then echo "pull $2 $3" >> "$TEST_SMB_LOG"; : > "$2"; exit 0; fi
exec "$(dirname "$0")/../bin/singularity" "$@"
SCRIPT
chmod +x "$sb/pull-bin/singularity"
out=$(env HOME="$sb/home" HPCLIB_DIR="$HPCLIB_DIR" HPCLIB_TUNNEL_PATH="$HPCLIB_DIR/tunnels" HPCTUNNELS_DATA_DIR="$sb/data" \
  PATH="$sb/pull-bin:$sb/bin:$CLEAN_PATH" TEST_SMB_LOG="$sb/log" bash "$HPCLIB_DIR/tunnels/setup_tunnel.sh" data-transfer \
  --set SMB_HOST=files.example.edu --set "SMB_IMAGE=$sb/images/dtt.sif" --save --install --force --check)
grep -q '^pull .*/dtt.sif.partial.[0-9]* docker://ghcr.io/tabor-research-group/data-transfer-tools:latest$' "$sb/log" ||
  fail "not pulled from ghcr.io: $(cat "$sb/log")"
case "$out" in *"HPCLIB_TUNNEL_STATUS installed"*) ;; *) fail "after the pull: $out" ;; esac

# smbshell --on runs it on the login node, with a terminal for a password prompt
(
  pssh() { printf '%s\n' "$@" > "$test_dir/smb-remote"; }
  smbshell --on -p 2222 me@login.example get proj/raw '/scratch/user/me/a b'
)
assert_equal "$(sed -n 1,4p "$test_dir/smb-remote" | tr '\n' ' ')" '-t -p 2222 me@login.example '
grep -q 'tunnels/data-transfer/smbshell.sh get proj/raw /scratch/user/me/a\\ b' "$test_dir/smb-remote" ||
  fail "remote: $(cat "$test_dir/smb-remote")"

echo 'Tunnel management tests passed'
