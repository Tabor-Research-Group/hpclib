#!/usr/bin/env bash
# One-time cluster setup for the ORCA scan demo, run from your own machine.
#
#   bash setup_cluster.sh [--rebuild] [ssh options] user@login.example /scratch/user/me/llm
#
# The last argument is an absolute directory on the cluster; the token is
# limited to it, and the scan is copied under it. This is `setup_agents`
# (hpclib/lib/tunnels.sh) with the demo's settings: the orca template and
# the writing_templates guide, cluster_config.json as the starting config,
# and a token named llm-scan saved as ~/.config/hpclib/llm_token. Jobs run
# sandboxed; --rebuild replaces the templates, config and tokens.
#
# Afterwards set the orca template's "modules" for your cluster (see README.md).
set -eo pipefail  # not -u: hpclib.sh predates it

if [ "$#" -lt 2 ]; then
  echo "usage: bash setup_cluster.sh [--rebuild] [ssh options] user@login.example REMOTE_WORK_DIR" >&2
  exit 2
fi
work_dir="${*: -1}"
login=("${@:1:$#-1}")
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=/dev/null
source "$here/../../hpclib.sh"
setup_agents --work-dir "$work_dir" --templates orca,writing_templates --config "$here/cluster_config.json" \
  --token-name "${TOKEN_NAME:-llm-scan}" --token-file "${TOKEN_FILE:-$HOME/.config/hpclib/llm_token}" \
  --owner-token-file "${OWNER_TOKEN_FILE:-$HOME/.config/hpclib/rest_token}" "${login[@]}"

cat <<'EOF2'
  - set "modules" in ~/.local/tunnels/rest/templates/orca/template.json on the cluster
    (e.g. run `module spider orca` there, or ask the model to use search_modules)
EOF2
