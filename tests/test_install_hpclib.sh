#!/usr/bin/env bash
# Tests for install_hpclib. A fake `ssh` runs the remote command locally
# inside a fake remote home directory, with stdin passed through.
set -e

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_dir="$(mktemp -d)"
test_dir="$(cd -P "$test_dir" && pwd)"
trap 'rm -rf "$test_dir"' EXIT

fail() { echo "FAIL: $*" >&2; exit 1; }
assert_equal() { [ "$1" = "$2" ] || fail "expected '$2', got '$1'"; }
assert_contains() { case "$1" in *"$2"*) ;; *) fail "expected output containing '$2', got: $1" ;; esac; }

# A local copy of the package, so the test can change its version and
# check that caches are left behind.
cp -R "$repo_dir/hpclib" "$test_dir/local-hpclib"
mkdir -p "$test_dir/local-hpclib/servers/__pycache__"
touch "$test_dir/local-hpclib/servers/__pycache__/junk.pyc"
HPCLIB_DIR="$test_dir/local-hpclib"
source "$HPCLIB_DIR/hpclib.sh"
local_version="$HPCLIB_VERSION"
[ -n "$local_version" ] || fail 'hpclib.sh does not set HPCLIB_VERSION'
assert_equal "$(_hpclib_read_version "$HPCLIB_DIR/hpclib.sh")" "$local_version"

set_version() {  # set_version FILE VERSION
  sed -i.bak "s/^HPCLIB_VERSION=.*/HPCLIB_VERSION=\"$2\"/" "$1" && rm -f "$1.bak"
}

# version comparison
assert_equal "$(_hpclib_version_compare 1.10.0 1.9.0)" 1
assert_equal "$(_hpclib_version_compare 1.2 1.2.0)" 0
assert_equal "$(_hpclib_version_compare 0.1.0 0.1.1)" -1
assert_equal "$(_hpclib_version_compare 2 10)" -1
assert_equal "$(_hpclib_version_compare 1.0.08 1.0.8)" 0
assert_equal "$(_hpclib_version_compare 1.0.b 1.0.a)" 1

remote_home="$test_dir/remote home"
mkdir -p "$remote_home" "$test_dir/bin"
cat > "$test_dir/bin/ssh" <<'SCRIPT'
#!/usr/bin/env bash
after_host=false
remote=''
for ssh_arg in "$@"; do
  if [ "$after_host" = true ]; then
    remote="$remote${remote:+ }$ssh_arg"
  elif [ "$ssh_arg" = login.example ] || [ "$ssh_arg" = me@login.example ]; then
    after_host=true
  fi
done
printf '%s\n' "$*" > "$TEST_SSH_ARGS_FILE"
cd "$TEST_REMOTE_HOME"
HOME="$TEST_REMOTE_HOME" PATH="$TEST_REMOTE_PATH" exec bash -c "$remote"
SCRIPT
chmod +x "$test_dir/bin/ssh"
TEST_REMOTE_HOME="$remote_home"
TEST_REMOTE_PATH="$PATH"
TEST_SSH_ARGS_FILE="$test_dir/ssh-args"
PATH="$test_dir/bin:$PATH"
HOME="$test_dir/local home"
mkdir -p "$HOME"
export TEST_REMOTE_HOME TEST_REMOTE_PATH TEST_SSH_ARGS_FILE PATH HOME

installed="$remote_home/hpclib"

# usage errors
if install_hpclib >/dev/null 2>&1; then fail 'accepted a missing host'; fi
if install_hpclib a.example b.example >/dev/null 2>&1; then fail 'accepted two hosts'; fi
if install_hpclib --target / login.example >/dev/null 2>&1; then fail 'accepted / as a target'; fi
if install_hpclib --target '~' login.example >/dev/null 2>&1; then fail 'accepted the home directory as a target'; fi

# --check on a fresh host changes nothing
out="$(install_hpclib --check login.example)"
assert_contains "$out" "would install hpclib $local_version"
[ ! -e "$installed" ] || fail '--check installed something'

# fresh install; ssh options pass through to ssh
out="$(install_hpclib -p 2222 me@login.example)"
assert_contains "$out" "installed hpclib $local_version"
assert_contains "$(cat "$TEST_SSH_ARGS_FILE")" '-p 2222'
assert_equal "$(_hpclib_read_version "$installed/hpclib.sh")" "$local_version"
[ -x "$installed/tunnels/start_tunnel.sh" ] || fail 'start_tunnel.sh missing or not executable'
[ -f "$installed/servers/rest_server.py" ] || fail 'servers were not installed'
[ ! -e "$installed/servers/__pycache__" ] || fail 'copied __pycache__'
[ ! -e "$installed.previous" ] || fail 'made a backup of nothing'
[ -z "$(ls -A "$remote_home" | grep '\.install\.')" ] || fail 'left a staging directory'
assert_equal "$(stat -c %a "$installed" 2>/dev/null || stat -f %Lp "$installed")" 755

# same version: nothing to do
touch "$installed/marker"
out="$(install_hpclib login.example)"
assert_contains "$out" 'is up to date'
[ -f "$installed/marker" ] || fail 'reinstalled an up-to-date copy'

# newer remote copy is left alone, unless forced
set_version "$installed/hpclib.sh" 9.0.0
out="$(install_hpclib login.example)"
assert_contains "$out" 'is newer than'
assert_equal "$(_hpclib_read_version "$installed/hpclib.sh")" 9.0.0
out="$(install_hpclib --check login.example)"
assert_contains "$out" 'is newer than'
out="$(install_hpclib --force login.example)"
assert_contains "$out" "installed hpclib $local_version"
assert_equal "$(_hpclib_read_version "$installed/hpclib.sh")" "$local_version"
assert_equal "$(_hpclib_read_version "$installed.previous/hpclib.sh")" 9.0.0

# older and unversioned remote copies are upgraded
set_version "$installed/hpclib.sh" 0.0.1
out="$(install_hpclib login.example)"
assert_contains "$out" 'replaced hpclib 0.0.1'
sed -i.bak '/^HPCLIB_VERSION=/d' "$installed/hpclib.sh" && rm -f "$installed/hpclib.sh.bak"
out="$(install_hpclib login.example)"
assert_contains "$out" 'replaced hpclib (unversioned)'
assert_equal "$(_hpclib_read_version "$installed/hpclib.sh")" "$local_version"

# a newer local version upgrades an up-to-date install
set_version "$HPCLIB_DIR/hpclib.sh" 99.1.0
out="$(install_hpclib login.example)"
assert_contains "$out" 'installed hpclib 99.1.0'
set_version "$HPCLIB_DIR/hpclib.sh" "$local_version"

# something that isn't hpclib is never replaced without --force
mkdir -p "$remote_home/notlib"
echo keep > "$remote_home/notlib/data.txt"
if install_hpclib --target notlib login.example > "$test_dir/out" 2>&1; then
  fail 'replaced a non-hpclib directory'
fi
assert_contains "$(cat "$test_dir/out")" 'is not an hpclib install'
assert_equal "$(cat "$remote_home/notlib/data.txt")" keep
install_hpclib --force --target notlib login.example >/dev/null
[ -f "$remote_home/notlib/hpclib.sh" ] || fail '--force did not install over a non-hpclib directory'
assert_equal "$(cat "$remote_home/notlib.previous/data.txt")" keep

# custom targets: ~/ paths, nested directories, absolute paths, spaces
install_hpclib --target '~/tools/my hpclib' login.example >/dev/null
[ -f "$remote_home/tools/my hpclib/hpclib.sh" ] || fail 'did not install to ~/tools/my hpclib'
install_hpclib --target="$test_dir/abs/hpclib" login.example >/dev/null
[ -f "$test_dir/abs/hpclib/hpclib.sh" ] || fail 'did not install to an absolute path'

# a broken upload leaves the existing install untouched
set_version "$HPCLIB_DIR/hpclib.sh" 99.2.0
cat > "$test_dir/bin/tar" <<'SCRIPT'
#!/usr/bin/env bash
echo 'not a tarball'
SCRIPT
chmod +x "$test_dir/bin/tar"
if install_hpclib login.example > "$test_dir/out" 2>&1; then fail 'reported success for a broken upload'; fi
rm "$test_dir/bin/tar"
assert_contains "$(cat "$test_dir/out")" 'is unchanged'
assert_equal "$(_hpclib_read_version "$installed/hpclib.sh")" 99.1.0
[ -z "$(ls -A "$remote_home" | grep '\.install\.')" ] || fail 'left a staging directory after a failure'
set_version "$HPCLIB_DIR/hpclib.sh" "$local_version"

# launch_tunnel runs start_tunnel.sh from HPCLIB_REMOTE_INSTALL_LOCATION
(
  HPCLIB_REMOTE_INSTALL_LOCATION='~/tools/my hpclib'
  _wait_for_port() { return 1; }
  pssh() { printf '%s\n' "${@: -1}" > "$test_dir/launch-command"; }
  launch_tunnel -P 5050 login.example flask >/dev/null 2>&1
)
eval "set -- $(cat "$test_dir/launch-command")"
assert_equal "$2" 'tools/my hpclib/tunnels/start_tunnel.sh'

echo 'install_hpclib tests passed'
