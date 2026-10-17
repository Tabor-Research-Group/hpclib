#!/usr/bin/env bash
# Real helper/keygen, fake SSH and SLURM; no connection to a real cluster.
set -e
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -rf "$test_dir"' EXIT
HPCLIB_DIR="$repo_dir/hpclib"
source "$HPCLIB_DIR/hpclib.sh"
HOME="$test_dir/shared home"
mkdir -p "$HOME/.ssh"
fail() { echo "FAIL: $*" >&2; exit 1; }
wait_for_job_node() { printf '%s\n' "$@" > "$test_dir/wait-args"; echo node7; }
ssh() { printf '%s\n' "$@" > "$test_dir/ssh-args"; }
touch "$HOME/.ssh/id_ed25519_hpclib_compute"
connect_to_job -ft -P 5999:5000,6000:5001 -R2 -S 0 -I0 -o 'ProxyCommand=ssh jump -W %h:%p' 123 'echo hi; echo "done"' >/dev/null
grep -Fx "$HOME/.ssh/id_ed25519_hpclib_compute" "$test_dir/ssh-args" >/dev/null || fail 'shared identity not selected'
grep -Fx 'ProxyCommand=ssh jump -W %h:%p' "$test_dir/ssh-args" >/dev/null || fail 'split SSH option with spaces'
grep -Fx 'echo hi; echo "done"' "$test_dir/ssh-args" >/dev/null || fail 'split shell command'
grep -Fx '127.0.0.1:6000:127.0.0.1:5001' "$test_dir/ssh-args" >/dev/null || fail 'lost second forward'
[ "$(cat "$test_dir/wait-args")" = "$(printf '%s\n' -R 2 -S 0 123)" ] || fail 'lost wait arguments'
connect_to_job -i "$test_dir/custom key" 123 >/dev/null
grep -Fx "$test_dir/custom key" "$test_dir/ssh-args" >/dev/null || fail 'split explicit identity'
grep -F 'id_ed25519_hpclib_compute' "$test_dir/ssh-args" >/dev/null && fail 'ignored explicit identity'
HPCLIB_COMPUTE_IDENTITY_FILE= connect_to_job 123 >/dev/null
grep -F 'id_ed25519_hpclib_compute' "$test_dir/ssh-args" >/dev/null && fail 'ignored disabled identity'
CONNECT_TO_JOB_SSH_ARGS=(-o 'IdentityFile=/a second key')
connect_to_job 123 printf '%s\n' "Mark's key with spaces" >/dev/null
grep -Fx 'IdentityFile=/a second key' "$test_dir/ssh-args" >/dev/null || fail 'split optional SSH array'
command=$(tail -n 1 "$test_dir/ssh-args")
[ "$(bash -c "$command")" = "Mark's key with spaces" ] || fail 'lost remote argument quoting'
unset CONNECT_TO_JOB_SSH_ARGS
pssh -p2222 -i "$test_dir/local key" -o 'ProxyCommand=ssh jump -W %h:%p' user@login.example 'echo hello; echo "world"'
grep -Fx "$test_dir/local key" "$test_dir/ssh-args" >/dev/null || fail 'pssh split identity'
grep -Fx 'echo hello; echo "world"' "$test_dir/ssh-args" >/dev/null || fail 'pssh split remote command'
if connect_to_job -i > /dev/null 2>&1; then fail 'accepted missing option value'; fi
if connect_to_job > /dev/null 2>&1; then fail 'accepted missing job'; fi
wait_for_job_node() { return 0; }
scancel() { printf '%s\n' "$@" > "$test_dir/cancel-args"; }
if connect_to_job 999 >/dev/null; then fail 'missing node succeeded'; fi
[ "$(cat "$test_dir/cancel-args")" = 999 ] || fail 'did not cancel timed out job'

# Capture and execute only the helper sent to a pretend login server.
# Use the configured Python launcher; do not invoke the test machine's default Python.
[ -n "$HPCLIB_TEST_PYTHON" ] || fail 'set HPCLIB_TEST_PYTHON to the test Python interpreter'
HPCTUNNELS_DATA_DIR="$test_dir/cluster data"
mkdir -p "$HPCTUNNELS_DATA_DIR/rest"
ln -s "$HPCLIB_TEST_PYTHON" "$HPCTUNNELS_DATA_DIR/rest/python"
ssh() {
  printf '%s\n' "$@" > "$test_dir/bootstrap-args"
  local remote
  for remote in "$@"; do :; done
  bash -c "$remote"
}
rm "$HOME/.ssh/id_ed25519_hpclib_compute"
export HOME HPCTUNNELS_DATA_DIR
setup_cluster_ssh -i "$test_dir/local key" user@login.example > "$test_dir/first.json"
setup_cluster_ssh user@login.example > "$test_dir/second.json"
grep '"created": true' "$test_dir/first.json" >/dev/null || fail 'setup did not generate compute key'
grep '"created": false' "$test_dir/second.json" >/dev/null || fail 'setup overwrote existing key'
grep 'PRIVATE KEY' "$test_dir/first.json" "$test_dir/second.json" >/dev/null && fail 'exported private key'
echo 'Cluster SSH tests passed'
