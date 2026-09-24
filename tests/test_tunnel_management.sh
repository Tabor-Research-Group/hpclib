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

echo 'Tunnel management tests passed'
