#!/bin/bash
# A transfer job, from `smbshell submit`: smbshell.sh with this job's arguments, or entry SLURM_ARRAY_TASK_ID of a
# manifest (--manifest FILE.jsonl, one JSON argument list per line). It never asks for a password: it signs in
# with your Kerberos ticket or the saved password (smbshell login / save-credentials).
set -o pipefail
dir="${SMBSHELL_DIR:?this job was submitted without SMBSHELL_DIR; submit it with smbshell submit}"
export SMB_NONINTERACTIVE=1
if [ "${1:-}" = --manifest ]; then
  mapfile -d '' args < <(python3 - "$2" "${SLURM_ARRAY_TASK_ID:?a manifest needs an array job}" <<'PY'
import json, sys
lines = open(sys.argv[1]).read().splitlines()
entry = json.loads(lines[int(sys.argv[2])])
sys.stdout.write("\0".join(entry) + "\0")
PY
  )
  [ "${#args[@]}" -gt 0 ] || { echo "smbshell: no entry ${SLURM_ARRAY_TASK_ID} in $2" >&2; exit 1; }
  set -- "${args[@]}"
fi
echo "smb transfer on $(hostname -s), job ${SLURM_JOB_ID:-?}${SLURM_ARRAY_TASK_ID:+ task $SLURM_ARRAY_TASK_ID}: smbshell $*"
exec bash "$dir/smbshell.sh" "$@"
