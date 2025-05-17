MAIN_USER=$(whoami)
MAIN_PARTITION=""

if [ -z "$HPCLIB_DIR" ]; then
  if [ "${BASH_SOURCE[0]}"="*/hpclib.sh" ];
    then HPCLIB_DIR=$(dirname "${BASH_SOURCE[0]}")
    else HPCLIB_DIR=$(dirname "$0")
  fi
fi

. $HPCLIB_DIR/lib/core.sh
. $HPCLIB_DIR/lib/connections.sh
. $HPCLIB_DIR/lib/slurm.sh
. $HPCLIB_DIR/lib/tunnels.sh
. $HPCLIB_DIR/lib/applications.sh
