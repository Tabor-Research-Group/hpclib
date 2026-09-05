if [ "${BASH_VERSINFO[0]}" -lt 3 ] || { [ "${BASH_VERSINFO[0]}" -eq 3 ] && [ "${BASH_VERSINFO[1]}" -lt 2 ]; }; then
  echo "hpclib requires bash >= 3.2 (found ${BASH_VERSION})" >&2
  return 1 2>/dev/null || exit 1
fi

if [ -z "$HPCLIB_DIR" ]; then
  HPCLIB_DIR=$(dirname "${BASH_SOURCE[0]}")/hpclib
fi

. $HPCLIB_DIR/hpclib.sh