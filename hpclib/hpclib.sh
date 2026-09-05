if [ "${BASH_VERSINFO[0]}" -lt 3 ] || { [ "${BASH_VERSINFO[0]}" -eq 3 ] && [ "${BASH_VERSINFO[1]}" -lt 2 ]; }; then
  echo "hpclib requires bash >= 3.2 (found ${BASH_VERSION})" >&2
  return 1 2>/dev/null || exit 1
fi

MAIN_USER=$(whoami)
MAIN_PARTITION=""

if [ -z "$HPCLIB_DIR" ]; then
  HPCLIB_DIR=$(dirname "${BASH_SOURCE[0]}")
fi

. $HPCLIB_DIR/lib/core.sh
. $HPCLIB_DIR/lib/connections.sh
. $HPCLIB_DIR/lib/slurm.sh
. $HPCLIB_DIR/lib/tunnels.sh
. $HPCLIB_DIR/lib/applications.sh
. $HPCLIB_DIR/lib/job_queue.sh