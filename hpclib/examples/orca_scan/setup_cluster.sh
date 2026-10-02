#!/usr/bin/env bash
# One-time cluster setup for the ORCA scan demo, run from your own machine.
#
#   bash setup_cluster.sh [ssh options] user@login.example /scratch/user/me/llm
#
# The last argument is an absolute directory on the cluster; the token is
# limited to it, and the scan is copied under it.
#
# 1. installs (or upgrades) hpclib on the cluster with install_hpclib
# 2. copies the bundled `orca` template and `writing_templates` guide into
#    ~/.local/tunnels/rest/templates (existing copies are left alone, since
#    you may have set the template's modules)
# 3. installs cluster_config.json as ~/.local/tunnels/rest/config.json if
#    there isn't one yet
# 4. creates the owner (full-access) token on this machine, saved as
#    ~/.config/hpclib/rest_token (mode 600), and gives the cluster only its
#    hash, so nothing there (a job included) can read it back
# 5. mints a scoped token (read, submit, propose, files:write) limited to the
#    given directory, saved locally as ~/.config/hpclib/llm_token (mode 600);
#    `propose` lets the model suggest template changes for you to approve
#
# Afterwards set the orca template's "modules" for your cluster (see README.md).
set -eo pipefail  # not -u: hpclib.sh predates it

if [ "$#" -lt 2 ]; then
  echo "usage: bash setup_cluster.sh [ssh options] user@login.example REMOTE_WORK_DIR" >&2
  exit 2
fi
work_dir="${*: -1}"
login=("${@:1:$#-1}")
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
token_name="${TOKEN_NAME:-llm-scan}"
token_file="${TOKEN_FILE:-$HOME/.config/hpclib/llm_token}"
owner_file="${OWNER_TOKEN_FILE:-$HOME/.config/hpclib/rest_token}"

# shellcheck source=/dev/null
source "$here/../../hpclib.sh"
remote_hpclib=$(_hpclib_remote_path "$HPCLIB_REMOTE_INSTALL_LOCATION")

echo "== installing hpclib"
install_hpclib "${login[@]}"

echo "== orca template and server config"
# The script goes over stdin (`bash -s`), with the config inlined, so the
# remote command line is plain words whatever the login shell is.
{
  cat <<'SCRIPT'
set -e
mkdir -p ~/.local/tunnels/rest/templates "$2"
for t in orca writing_templates; do
  if [ ! -e ~/.local/tunnels/rest/templates/$t ]; then
    cp -R "$1/tunnels/rest/templates/$t" ~/.local/tunnels/rest/templates/
    echo "installed $t"
  else
    echo "kept the existing $t"
  fi
done
if [ -e ~/.local/tunnels/rest/config.json ]; then
  echo "kept the existing config.json"
  exit 0
fi
cat > ~/.local/tunnels/rest/config.json <<'HPCLIB_CONFIG'
SCRIPT
  cat "$here/cluster_config.json"
  printf '%s\n' 'HPCLIB_CONFIG' 'echo "installed config.json"'
} | HPCLIB_ECHO_COMMANDS= pssh "${login[@]}" "$(printf '%q ' bash -s -- "$remote_hpclib" "$work_dir")"

echo "== owner token"
# The REST server reads its owner token from the cluster's
# ~/.local/tunnels/rest_token, creating a plaintext one on first start if
# there is none. Creating it here instead means the cluster only ever
# holds `sha256:<hash>`.
owner_script='
f="${HPCTUNNELS_DATA_DIR:-$HOME/.local/tunnels}/rest_token"
if [ -e "$f" ]; then
  if head -c 7 "$f" | grep -q "^sha256:"; then echo hashed; else echo plaintext; fi
elif [ "$1" = install ]; then
  mkdir -p "$(dirname "$f")"
  (umask 077; set -o noclobber; printf "sha256:%s\n" "$2" > "$f")
  echo installed
else
  echo missing
fi'
owner_state() {  # owner_state check | owner_state install HASH
  printf '%s\n' "$owner_script" | HPCLIB_ECHO_COMMANDS= pssh "${login[@]}" "$(printf '%q ' bash -s -- "$@")" | tail -n 1
}
state=$(owner_state check)
case "$state" in
  missing)
    if [ ! -e "$owner_file" ]; then
      mkdir -p "$(dirname "$owner_file")"
      (umask 077; python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > "$owner_file")
      echo "created $owner_file"
    fi
    hash=$(python3 -c 'import hashlib, sys; print(hashlib.sha256(sys.stdin.read().strip().encode()).hexdigest())' \
      < "$owner_file")
    [ "$(owner_state install "$hash")" = installed ] || { echo "could not install the owner token's hash" >&2; exit 1; }
    echo "the cluster has the hash of $owner_file; keep that file, it is the only copy of the token"
    ;;
  hashed)
    echo "the cluster already has a hashed owner token"
    if [ ! -e "$owner_file" ]; then
      echo "  ($owner_file doesn't exist; if you have lost the token, delete ~/.local/tunnels/rest_token on the"
      echo "   cluster and rerun this script to make a new one)"
    fi
    ;;
  plaintext)
    echo "the cluster has a plaintext owner token, made when the REST server first started. To hash it:"
    echo "  (umask 077; ssh ${login[*]} cat .local/tunnels/rest_token > $owner_file)"
    echo "  ssh ${login[*]} python3 $remote_hpclib/servers/rest_server.py --hash-token-file"
    ;;
  *)
    echo "could not check the owner token on the cluster (got: $state)" >&2
    exit 1
    ;;
esac

echo "== scoped token '$token_name' for $work_dir"
if [ -e "$token_file" ]; then
  echo "$token_file already exists; to mint a new one (e.g. to add the propose scope), run"
  echo "  ssh ${login[*]} python3 $remote_hpclib/servers/rest_server.py --revoke-token $token_name"
  echo "  rm $token_file"
  echo "and rerun this script"
else
  mkdir -p "$(dirname "$token_file")"
  (umask 077; : > "$token_file")
  printf -v mint '%q ' python3 "$remote_hpclib/servers/rest_server.py" --add-token "$token_name" \
    --scopes read,submit,propose,files:write --token-allow "$work_dir"
  if ! HPCLIB_ECHO_COMMANDS= pssh "${login[@]}" "$mint" < /dev/null > "$token_file"; then
    rm -f "$token_file"
    exit 1
  fi
  echo "saved to $token_file"
fi

cat <<EOF

Next:
  - set "modules" in ~/.local/tunnels/rest/templates/orca/template.json on the cluster
    (e.g. run \`module spider orca\` there, or ask the model to use search_modules)
  - start the tunnel:  launch_tunnel -A none -P 5050 ${login[*]} rest -- --allow $work_dir
EOF
